#include <map>
#include <set>
#include "Common.h"
#include "Allocs.h"

#define DEBUG_TYPE "allocs"

using namespace llvm;

//static cl::list<std::string> MallocWrappers("malloc-wrapper",
//        cl::desc("Add custom malloc function name"));

static cl::opt<bool> ClOnDemand("allocs-ondemand",
        cl::desc("Do not scan for allocations, find them later with getAllocSite instead"),
        cl::init(false));

static std::map<std::string, AllocInfo> AllocFuncs = {
    { "malloc",                          { AllocInfo::Malloc,  0, -1, false } },
    { "valloc",                          { AllocInfo::Malloc,  0, -1, false } },
    { "pvalloc",                         { AllocInfo::Malloc,  0, -1, false } },
    { "aligned_alloc",                   { AllocInfo::Malloc,  1, -1, false } },
    { "memalign",                        { AllocInfo::Malloc,  1, -1, false } },
    // TODO: posix_memalign
    { "calloc",                          { AllocInfo::Calloc,  1,  0, false } },
    { "realloc",                         { AllocInfo::Realloc, 1, -1, false } },
    { "reallocf",                        { AllocInfo::Realloc, 1, -1, false } },
    { "reallocarray",                    { AllocInfo::Realloc, 2,  1, false } },
    { "_Znwj",                           { AllocInfo::New,     0, -1, false } }, // new(uint)
    { "_ZnwjRKSt9nothrow_t",             { AllocInfo::Malloc,  0, -1, false } },
    { "_Znwm",                           { AllocInfo::New,     0, -1, false } }, // new(ulong)
    { "_ZnwmRKSt9nothrow_t",             { AllocInfo::Malloc,  0, -1, false } },
    { "_Znaj",                           { AllocInfo::New,     0, -1, false } }, // new[](uint)
    { "_ZnajRKSt9nothrow_t",             { AllocInfo::Malloc,  0, -1, false } },
    { "_Znam",                           { AllocInfo::New,     0, -1, false } }, // new[](ulong)
    { "_ZnamRKSt9nothrow_t",             { AllocInfo::Malloc,  0, -1, false } },
    { "msvc_new_int",                    { AllocInfo::New,     0, -1, false } },
    { "msvc_new_int_nothrow",            { AllocInfo::Malloc,  0, -1, false } },
    { "msvc_new_longlong",               { AllocInfo::New,     0, -1, false } },
    { "msvc_new_longlong_nothrow",       { AllocInfo::Malloc,  0, -1, false } },
    { "msvc_new_array_int",              { AllocInfo::New,     0, -1, false } },
    { "msvc_new_array_int_nothrow",      { AllocInfo::Malloc,  0, -1, false } },
    { "msvc_new_array_longlong",         { AllocInfo::New,     0, -1, false } },
    { "msvc_new_array_longlong_nothrow", { AllocInfo::Malloc,  0, -1, false } },
    { "strdup",                          { AllocInfo::StrDup, -1, -1, false } },
    { "strndup",                         { AllocInfo::StrDup, -1, -1, false } },
    // TODO: __cxa_allocate_exception
    { "free",                            { AllocInfo::Free,   -1, -1, false } },
    // TODO: delete
};

/* Allocation sites */

AllocSite *AllocSite::TryCreate(Value *V) {
    if (GlobalVariable *GV = dyn_cast<GlobalVariable>(V))
        return new AllocSite(*GV);

    if (AllocaInst *AI = dyn_cast<AllocaInst>(V))
        return new AllocSite(*AI);

    if (Instruction *I = dyn_cast<Instruction>(V)) {
        CallSite CS(I);
        if (CS) {
            Function *Callee = CS.getCalledFunction();
            if (Callee && Callee->hasName()) {
                auto it = AllocFuncs.find(Callee->getName().str());
                if (it != AllocFuncs.end())
                    return new AllocSite(*I, it->second);
            }
        }
    }

    return nullptr;
}

Constant *AllocSite::getSizeInt(uint64_t N) {
    return ConstantInt::get(DL.getLargestLegalIntType(V->getContext()), N);
}

SmallVector<Value*, 2> AllocSite::getSizeFactors() {
    assert(isAnyAlloc());
    SmallVector<Value*, 2> Factors;

    if (isGlobalAlloc()) {
        Factors.push_back(getSizeInt(DL.getTypeStoreSize(V->getType()->getPointerElementType())));
    } else if (isStackAlloc()) {
        AllocaInst *AI = cast<AllocaInst>(V);
        Factors.push_back(getSizeInt(DL.getTypeAllocSize(AI->getAllocatedType())));
        if (AI->isArrayAllocation())
            Factors.push_back(AI->getArraySize());
    } else {
        CallSite CS(V);
        assert(CS);
        if (Info.SizeArg >= 0)
            Factors.push_back(CS.getArgOperand(Info.SizeArg));
        if (Info.MembArg >= 0)
            Factors.push_back(CS.getArgOperand(Info.MembArg));
    }

    return std::move(Factors);
}

Value *AllocSite::getCallParam(uint64_t i) {
    CallSite CS(V);
    assert(CS);
    return CS.getArgOperand(i);
}

uint64_t AllocSite::getConstSize() {
    assert(isAnyAlloc());
    SmallVector<Value*, 2> MulOps = getSizeFactors();

    uint64_t Size = 1;

    for (Value *Op : MulOps) {
        ConstantInt *C = dyn_cast<ConstantInt>(Op);
        if (!C)
            return UnknownSize;
        Size *= C->getZExtValue();
    }

    return Size;
}

Value *AllocSite::getOrInsertSize(bool *Changed) {
    assert(isAnyAlloc() && !isStrDup());

    if (Changed)
        *Changed = false;

    uint64_t ConstSize = getConstSize();
    if (ConstSize != UnknownSize)
        return getSizeInt(ConstSize);

    SmallVector<Value*, 2> MulOps = getSizeFactors();

    switch (MulOps.size()) {
        case 0: return nullptr;
        case 1: return MulOps[0];
        case 2: {
            IRBuilder<> B(cast<Instruction>(V));
            if (Changed)
                *Changed = true;
            return B.CreateMul(MulOps[0], MulOps[1], "bytesize");
        }
        default:
            assert(!"impossible # of factors");
    }
}

Value *AllocSite::getFreedPointer() {
    assert(isAnyFree());
    CallSite CS(V);
    return CS.getArgOperand(0);
}

Value *AllocSite::getRellocatedPointer() {
    assert(isRealloc());
    return cast<CallInst>(V)->getArgOperand(0);
}

const SCEV *AllocSite::getSizeSCEV(ScalarEvolution &SE) {
    if (isGlobalAlloc()) {
        return SE.getSizeOfExpr(
            DL.getLargestLegalIntType(V->getContext()),
            V->getType()->getPointerElementType()
        );
    }

    SmallVector<Value*, 2> Ops = getSizeFactors();

    switch (Ops.size()) {
        case 0:  return nullptr;
        case 1:  return SE.getSCEV(Ops[0]);
        case 2:  return SE.getMulExpr(SE.getSCEV(Ops[0]), SE.getSCEV(Ops[1]), SCEV::FlagNUW);
        default: assert(!"impossible # of factors");
    }
}

const SCEV *AllocSite::getEndSCEV(ScalarEvolution &SE) {
    assert(isAnyAlloc());
    if (const SCEV *Start = SE.getSCEV(V)) {
        if (const SCEV *Size = getSizeSCEV(SE))
            return SE.getAddExpr(Start, Size, SCEV::FlagNUW);
    }
    return nullptr;
}

/* Analysis pass */

AllocsPass::site_range AllocsPass::sites() {
    assert(!ClOnDemand && !"iteration not available in on-demand mode");
    auto B = FuncSites.begin(), E = FuncSites.end();
    return site_range(site_iterator(B, E), site_iterator(E, E));
}

AllocsPass::site_range AllocsPass::func_sites(Function *F) {
    assert(!ClOnDemand && !"iteration not available in on-demand mode");
    auto B = FuncSites.find(F), E = B;
    if (E != FuncSites.end()) ++E;
    return site_range(site_iterator(B, E), site_iterator(E, E));
}

AllocSite *AllocsPass::getAllocSite(Value *V) {
    auto it = SiteLookup.find(V);
    if (it != SiteLookup.end())
        return it->second;

    if (ClOnDemand) {
        AllocSite *A = AllocSite::TryCreate(V);
        if (A)
            SiteLookup[V] = A;
        return A;
    }

    return nullptr;
}

bool AllocsPass::runOnModule(Module &M) {
    // TODO: ignore noinstrument globals/functions
    DL = &M.getDataLayout();

    // TODO: register custom wrappers

    if (ClOnDemand)
        return false;

    // Global allocations are stored under NULL function
    SiteList &GlobalAllocs = FuncSites[nullptr];
    for (GlobalVariable &GV : M.globals()) {
        GlobalAllocs.push_back(new AllocSite(GV));
        SiteLookup[&GV] = GlobalAllocs.back();
    }

    // Local allocations/frees are stored under parent function
    for (Function &F : M) {
        SiteList &Sites = FuncSites[&F];

        for (Instruction &I : instructions(F)) {
            if (AllocSite *A = AllocSite::TryCreate(&I)) {
                Sites.push_back(A);
                SiteLookup[&I] = A;
            }
        }
    }

    bool Changed = false;

    if (DebugFlag) {
        for (AllocSite &A : sites()) {
            if (A.isGlobalAlloc()) dbgs() << "global";
            if (A.isStackAlloc())  dbgs() << "stack";
            if (A.isHeapAlloc())   dbgs() << "heap";
            if (A.isAnyAlloc())    dbgs() << " alloc";
            if (A.isAnyFree())     dbgs() << "free";
            if (A.isMalloc())      dbgs() << " (malloc)";
            if (A.isCalloc())      dbgs() << " (calloc)";
            if (A.isRealloc())     dbgs() << " (realloc)";
            if (A.isStrDup())      dbgs() << " (strdup)";
            if (A.isNew())         dbgs() << " (new)";
            if (A.isDelete())      dbgs() << " (delete)";
            if (A.isWrapper())     dbgs() << " (wrapper)";
            dbgs() << ": " << *A.getValue() << "\n";

            if (A.isAnyAlloc()) {
                if (Value *Size = A.getOrInsertSize(&Changed))
                    dbgs() << "  byte size: " << *Size << "\n";
            }
        }

        for (Function &F : M) {
            for (Instruction &I : instructions(F)) {
                if (LoadInst *LI = dyn_cast<LoadInst>(&I)) {
                    if (isInBounds(*LI)) {
                        dbgs() << "in-bounds load: " << I << "\n";
                        dbgs() << "  pointer: " << *LI->getPointerOperand() << "\n";
                    }
                }
                else if (StoreInst *SI = dyn_cast<StoreInst>(&I)) {
                    if (isInBounds(*SI)) {
                        dbgs() << "in-bounds store: " << I << "\n";
                        dbgs() << "  pointer: " << *SI->getPointerOperand() << "\n";
                    }
                }
            }
        }
    }

    return Changed;
}

void AllocsPass::getAnalysisUsage(AnalysisUsage &AU) const {
    AU.setPreservesAll();
}

/* Bounds analysis */

SizeOffsetType AllocsPass::computeSizeAndOffset(Value *Addr) {
    // TODO: (array) parameters with constant size
    unsigned PtrBitWidth = DL->getPointerSizeInBits();
    APInt Offset(PtrBitWidth, 0);
    Addr = Addr->stripAndAccumulateInBoundsConstantOffsets(*DL, Offset);
    if (AllocSite *A = getAllocSite(Addr)) {
        uint64_t Size = A->getConstSize();
        if (Size != AllocSite::UnknownSize)
            return std::make_pair(APInt(PtrBitWidth, Size), Offset);
    }
    return std::make_pair(APInt(), APInt()); // ObjectSizeOffsetVisitor::unknown()
}

// Copied from AddressSanitizer::isSafeAccess
bool AllocsPass::isInBoundsAccess(Value *Addr, uint64_t AccessedBytes) {
    SizeOffsetType SizeOffset = computeSizeAndOffset(Addr);
    if (!ObjectSizeOffsetVisitor::bothKnown(SizeOffset))
        return false;

    uint64_t Size = SizeOffset.first.getZExtValue();
    int64_t Offset = SizeOffset.second.getSExtValue();

    // Three checks are required to ensure safety:
    // . Offset >= 0  (since the offset is given from the base ptr)
    // . Size >= Offset  (unsigned)
    // . Size - Offset >= NeededSize  (unsigned)
    return Offset >= 0 && Size >= uint64_t(Offset) &&
           Size - uint64_t(Offset) >= AccessedBytes;
}

bool AllocsPass::isInBounds(LoadInst &LI) {
    return isInBoundsAccess(LI.getPointerOperand(),
            DL->getTypeStoreSize(LI.getType()));
}

bool AllocsPass::isInBounds(StoreInst &SI) {
    return isInBoundsAccess(SI.getPointerOperand(),
            DL->getTypeStoreSize(SI.getValueOperand()->getType()));
}

char AllocsPass::ID = 0;
static RegisterPass<AllocsPass> X("allocs",
        "Find allocations (stack + heap + global) and frees (heap)",
        false, true);
