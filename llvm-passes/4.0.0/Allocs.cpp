#include <map>
#include <set>
#include "Common.h"
#include "Allocs.h"

#define DEBUG_TYPE "allocs"

using namespace llvm;

//static cl::list<std::string> MallocWrappers("malloc-wrapper",
//        cl::desc("Add custom malloc function name"));

static cl::opt<bool> ShowDebugOutput("allocs-debug",
        cl::desc("Show all found allocations in debug output"),
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

AllocSite *AllocSite::TryCreate(Instruction &I) {
    CallSite CS(&I);
    if (CS) {
        Function *Callee = CS.getCalledFunction();
        if (Callee && Callee->hasName()) {
            auto it = AllocFuncs.find(Callee->getName().str());
            if (it != AllocFuncs.end())
                return new AllocSite(I, it->second);
        }
    }
    return nullptr;
}

Constant *AllocSite::getSizeInt(uint64_t N) {
    return ConstantInt::get(DL.getLargestLegalIntType(V->getContext()), N);
}

SmallVector<Value*, 2> AllocSite::getSizeFactors() {
    assert(isAnyAlloc());
    SmallVector<Value*, 2> MulOps;
if (isGlobalAlloc()) {
        MulOps.push_back(getSizeInt(DL.getTypeStoreSize(V->getType()->getPointerElementType())));
    } else if (isStackAlloc()) {
        AllocaInst *AI = cast<AllocaInst>(V);
        MulOps.push_back(getSizeInt(DL.getTypeAllocSize(AI->getAllocatedType())));
        if (AI->isArrayAllocation())
            MulOps.push_back(AI->getArraySize());
    } else {
        CallSite CS(V);
        assert(CS);
        if (Info.SizeArg >= 0)
            MulOps.push_back(CS.getArgOperand(Info.SizeArg));
        if (Info.MembArg >= 0)
            MulOps.push_back(CS.getArgOperand(Info.MembArg));
    }

    return std::move(MulOps);
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
            assert(!"impossible # of mulops");
    }

    return nullptr;
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
    assert(isAnyAlloc());
    return nullptr;
}

const SCEV *AllocSite::getEndSCEV(ScalarEvolution &SE) {
    assert(isAnyAlloc());
    return nullptr;
}

/* Analysis pass */

AllocsPass::site_range AllocsPass::sites() {
    auto B = FuncSites.begin(), E = FuncSites.end();
    return site_range(site_iterator(B, E), site_iterator(E, E));
}

AllocsPass::site_range AllocsPass::func_sites(Function *F) {
    auto B = FuncSites.find(F), E = B;
    if (E != FuncSites.end()) ++E;
    return site_range(site_iterator(B, E), site_iterator(E, E));
}

bool AllocsPass::runOnModule(Module &M) {
    // TODO: register custom wrappers

    // Global allocations are stored under NULL function
    SiteList &GlobalAllocs = FuncSites.FindAndConstruct(nullptr).second;
    for (GlobalVariable &GV : M.globals())
        GlobalAllocs[&GV] = new AllocSite(GV);

    // Local allocations/frees are stored under parent function
    for (Function &F : M) {
        SiteList &Sites = FuncSites.FindAndConstruct(&F).second;

        for (Instruction &I : instructions(F)) {
            if (AllocSite *A = AllocSite::TryCreate(I))
                Sites[&I] = A;
        }
    }

    bool Changed = false;

    if (ShowDebugOutput) {
        for (AllocSite *A : sites()) {
            if (A->isGlobalAlloc())
                dbgs() << "global";
            if (A->isStackAlloc())
                dbgs() << "stack";
            if (A->isHeapAlloc())
                dbgs() << "heap";
            if (A->isAnyAlloc())
                dbgs() << " alloc";
            if (A->isAnyFree())
                dbgs() << "free";
            if (A->isMalloc())
                dbgs() << " (malloc)";
            if (A->isCalloc())
                dbgs() << " (calloc)";
            if (A->isRealloc())
                dbgs() << " (realloc)";
            if (A->isStrDup())
                dbgs() << " (strdup)";
            if (A->isNew())
                dbgs() << " (new)";
            if (A->isDelete())
                dbgs() << " (delete)";
            if (A->isWrapper())
                dbgs() << " (wrapper)";
            dbgs() << ": " << *A->getValue() << "\n";

            if (A->isAnyAlloc()) {
                dbgs() << "  byte size: " << *A->getOrInsertSize(&Changed) << "\n";
            }
        }
    }

    return Changed;
}

void AllocsPass::getAnalysisUsage(AnalysisUsage &AU) const {
    AU.setPreservesAll();
}

void AllocsPass::freeSites() {
    for (auto &it : FuncSites) {
        for (auto &iit : it.second)
            delete iit.second;
    }
}

char AllocsPass::ID = 0;
static RegisterPass<AllocsPass> X("allocs",
        "Find allocations (stack + heap + global) and frees (heap)",
        false, true);
