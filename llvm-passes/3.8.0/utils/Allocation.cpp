#include "utils/Common.h"
#include "utils/Allocation.h"

#define DEBUG_TYPE "allocation"

using namespace llvm;

/*
 * TODO:
 * Rewrite to something like
 * http://monica.clients.vu.nl/llvm/MemoryBuiltins_8h.html
 */

/* TODO: str[n]dup, [posix_]memalign, msvc new ops */
static std::map<std::string, int> MallocFuncs = {
    { "malloc", 0 },
    { "valloc", 0 },
    { "_Znwj", 0 }, /* new(unsigned int) */
    { "_ZnwjRKSt9nothrow_t", 0 },
    { "_Znwm", 0 }, /* new(unsigned long) */
    { "_ZnwmRKSt9nothrow_t", 0 },
    { "_Znaj", 0 }, /* new[](unsigned int) */
    { "_ZnajRKSt9nothrow_t", 0 },
    { "_Znam", 0 }, /* new[](unsigned long) */
    { "_ZnamRKSt9nothrow_t", 0 },

    /* C++ exception support */
    /* XXX do we want to include this? we don't propagate this later. */
    /* XXX this buffer extends below the returned pointer. */
    { "__cxa_allocate_exception", 0 },
};

static std::map<std::string, int> MallocWrappers = {
    /* custom pool allocators
     * This does not include direct malloc() wrappers such as xmalloc, only
     * functions that allocate a large memory pool once and then perform small
     * allocations in that pool. */
    { "ggc_alloc", 0 },             // gcc
    { "alloc_anon", 1 },            // gcc
    { "ngx_alloc", 0 },             // nginx
    { "ngx_palloc", 1 },            // nginx
    { "ngx_palloc_small", 1 },      // nginx ngx_palloc inline
    { "ngx_palloc_large", 1 },      // nginx ngx_palloc inline
};

static std::set<std::string> CallocFuncs = {
    "calloc",
};

static std::set<std::string> CallocWrappers = {
};

static std::set<std::string> ReallocFuncs = {
    "realloc",
    "reallocf",
};

static std::set<std::string> ReallocWrappers = {
};

static std::set<std::string> FreeFuncs = {
    "free",
};

static std::set<std::string> FreeWrappers = {
};

static inline bool isInList(std::map<std::string, int> Map, Function *F) {
    return Map.find(F->getName().str()) != Map.end();
}

static inline bool isInList(std::set<std::string> Set, Function *F) {
    return Set.count(F->getName().str()) > 0;
}

bool isMalloc(Function *F) {
    return isInList(MallocFuncs, F);
}

bool isCalloc(Function *F) {
    return isInList(CallocFuncs, F);
}

bool isRealloc(Function *F) {
    return isInList(ReallocFuncs, F);
}

bool isFree(Function *F) {
    return isInList(FreeFuncs, F);
}

bool isMallocWrapper(Function *F) {
    return isInList(MallocWrappers, F);
}

bool isCallocWrapper(Function *F) {
    return isInList(CallocWrappers, F);
}

bool isReallocWrapper(Function *F) {
    return isInList(ReallocWrappers, F);
}

bool isFreeWrapper(Function *F) {
    return isInList(FreeWrappers, F);
}

bool isAllocationFunc(Function *F) {
    return isMalloc(F) || isCalloc(F) || isRealloc(F) ||
        isMallocWrapper(F) || isCallocWrapper(F) || isReallocWrapper(F);
}

bool isFreeFunc(Function *F) {
    return isFree(F) || isFreeWrapper(F);
}

int getSizeArg(Function *F) {
    const std::string &name = F->getName().str();

    auto it = MallocFuncs.find(name);
    if (it != MallocFuncs.end())
        return it->second;

    it = MallocWrappers.find(name);
    if (it != MallocWrappers.end())
        return it->second;

    return -1;
}

/*
 * Insert object size in pointers after allocations.
 *
 * Partially reimplements MemoryBuiltins.cpp from llvm to detect allocators.
 */
static bool isHeapAllocation(CallSite &CS,
        AllocationSite::AllocationType &CallType,
        bool &IsWrapped) {
    Function *F = CS.getCalledFunction();

    if (!F || !F->hasName() || F->isIntrinsic())
        return false;

    // XXX removed the ParentFunc check here in favor of source patches, still
    // need a source patch for ngx_set_environment

    if (isMalloc(F)) {
        CallType = AllocationSite::Malloc;
        IsWrapped = false;
    } else if (isCalloc(F)) {
        CallType = AllocationSite::Calloc;
        IsWrapped = false;
    } else if (isRealloc(F)) {
        CallType = AllocationSite::Realloc;
        IsWrapped = false;
    } else if (isMallocWrapper(F)) {
        CallType = AllocationSite::Malloc;
        IsWrapped = true;
    } else if (isCallocWrapper(F)) {
        CallType = AllocationSite::Calloc;
        IsWrapped = true;
    } else if (isReallocWrapper(F)) {
        CallType = AllocationSite::Realloc;
        IsWrapped = true;
    } else {
        return false;
    }

    return true;
}

bool isAllocation(Instruction *I, AllocationSite &AS) {
    if (!I)
        return false;

    if (isa<AllocaInst>(I)) {
        AS.Allocation = I;
        AS.CallType = AllocationSite::Alloca;
        AS.SizeArg = -1;
        AS.IsWrapped = false;
        return true;
    }

    if (isa<CallInst>(I) || isa<InvokeInst>(I)) {
        CallSite CS(I);
        if (isHeapAllocation(CS, AS.CallType, AS.IsWrapped)) {
            AS.Allocation = I;
            if (AS.CallType == AllocationSite::Malloc) {
                Function *F = CS.getCalledFunction();
                AS.SizeArg = getSizeArg(F);
            }
            else if (AS.CallType == AllocationSite::Realloc) {
                AS.SizeArg = 1;
            }
            return true;
        }
    }

    return false;
}

Value *AllocationSite::instrumentWithByteSize(IRBuilder<> &B, const DataLayout &DL) {
    switch (CallType) {
    case Malloc:
    case Realloc: {
        CallSite CS(Allocation);
        return CS.getArgOperand(SizeArg);
    }
    case Calloc: {
        CallSite CS(Allocation);
        Value *NumElements = CS.getArgOperand(0);
        Value *ElementSize = CS.getArgOperand(1);
        return B.CreateMul(NumElements, ElementSize);
    }
    case Alloca: {
        AllocaInst *AI = cast<AllocaInst>(Allocation);
        Value *Size = B.getInt64(DL.getTypeAllocSize(AI->getAllocatedType()));

        if (AI->isArrayAllocation())
            Size = B.CreateMul(Size, AI->getArraySize());

        return Size;
    }
    }
    return nullptr; /* never reached */
}

size_t AllocationSite::getConstSize(const DataLayout &DL) {
    switch (CallType) {
    case Malloc:
    case Realloc: {
        CallSite CS(Allocation);
        ifcast(ConstantInt, C, CS.getArgOperand(SizeArg))
            return C->getZExtValue();
        break;
    }
    case Calloc: {
        CallSite CS(Allocation);
        ifcast(ConstantInt, NumElements, CS.getArgOperand(0)) {
            ifcast(ConstantInt, ElementSize, CS.getArgOperand(1))
                return NumElements->getZExtValue() * ElementSize->getZExtValue();
        }
        break;
    }
    case Alloca: {
        AllocaInst *AI = cast<AllocaInst>(Allocation);
        size_t Size = DL.getTypeAllocSize(AI->getAllocatedType());

        if (AI->isArrayAllocation()) {
            ifncast(ConstantInt, ConstArraySize, AI->getArraySize())
                break;
            Size *= ConstArraySize->getZExtValue();
        }

        return Size;
    }
    }
    return NoSize;
}

const SCEV *AllocationSite::getSizeSCEV(ScalarEvolution &SE) {
    switch (CallType) {
    case Malloc:
    case Realloc:
        return SE.getSCEV(CallSite(Allocation).getArgOperand(SizeArg));
    case Calloc: {
        CallSite CS(Allocation);
        Value *NumElements = CS.getArgOperand(0);
        Value *ElementSize = CS.getArgOperand(1);
        return SE.getMulExpr(SE.getSCEV(NumElements), SE.getSCEV(ElementSize), SCEV::FlagNUW);
    }
    case Alloca: {
        AllocaInst *AI = cast<AllocaInst>(Allocation);
        IntegerType *i64Ty = Type::getInt64Ty(AI->getContext());
        const SCEV *Size = SE.getSizeOfExpr(i64Ty, AI->getAllocatedType());

        if (AI->isArrayAllocation())
            Size = SE.getMulExpr(Size, SE.getSCEV(AI->getArraySize()), SCEV::FlagNUW);

        return Size;
    }
    }
    return nullptr;
}

const SCEV *AllocationSite::getEndPointerSCEV(ScalarEvolution &SE) {
    return SE.getAddExpr(SE.getSCEV(Allocation), getSizeSCEV(SE), SCEV::FlagNUW);
}

const SCEV *getGlobalSizeSCEV(GlobalVariable *GV, ScalarEvolution &SE) {
    return SE.getSizeOfExpr(Type::getInt64Ty(GV->getContext()),
            GV->getType()->getPointerElementType());
}
