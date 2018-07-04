#ifndef ALLOCS_H
#define ALLOCS_H

#include <llvm/Pass.h>
#include <llvm/IR/Value.h>
#include <llvm/IR/Module.h>
#include <llvm/IR/Function.h>
#include <llvm/IR/Instruction.h>
#include <llvm/IR/Instructions.h>
#include <llvm/ADT/DenseMap.h>
#include <llvm/ADT/MapVector.h>
#include <llvm/ADT/APInt.h>
#include <llvm/Analysis/ScalarEvolution.h>
#include <llvm/Analysis/MemoryBuiltins.h>

using namespace llvm;

struct AllocInfo {
    // This list is inspired by MemoryBuiltins.h from the LLVM source
    enum AllocType : unsigned short {
        Malloc      = (1 << 0),  // alloc, may return NULL
        New         = (1 << 1),  // always alloc
        Calloc      = (1 << 2),  // alloc + zeroinit
        StrDup      = (1 << 3),  // str[n]dup
        Realloc     = (1 << 4),  // reallocs
        Free        = (1 << 5),  // frees, may accept NULL
        Delete      = (1 << 6),  // frees only non-NULL
        Alloca      = (1 << 7),  // stack allocations
        Global      = (1 << 8),  // globals
        HeapAlloc   = Malloc | New | Calloc | StrDup | Realloc,
        AnyAlloc    = HeapAlloc | Alloca | Global,
        AnyFree     = Free | Delete
    };

    AllocType Type;
    int MembArg;
    int SizeArg;
    bool IsWrapper;
};

struct AllocSite {
    static const uint64_t UnknownSize = (uint64_t)(-1LL);

private:
    Value *V;
    AllocInfo Info;
    const DataLayout &DL;

    AllocSite() = default;
    AllocSite(Value *V, AllocInfo Info, Module *M)
        : V(V), Info(Info), DL(M->getDataLayout()) {}

    Constant *getSizeInt(uint64_t N);
    SmallVector<Value*, 2> getSizeFactors();

public:
    AllocSite(AllocaInst &AI)
        : AllocSite(&AI, { AllocInfo::Alloca, -1, -1, false }, AI.getModule()) {}
    AllocSite(GlobalVariable &GV)
        : AllocSite(&GV, { AllocInfo::Global, -1, -1, false }, GV.getParent()) {}
    AllocSite(Instruction &I, AllocInfo Info)
        : AllocSite(&I, Info, I.getModule()) {}

    static AllocSite *TryCreate(Value *V);

    inline Value *getValue()              { return V; }
    inline AllocInfo::AllocType getType() { return Info.Type; }

    inline bool isMalloc()      { return Info.Type & AllocInfo::Malloc; }
    inline bool isCalloc()      { return Info.Type & AllocInfo::Calloc; }
    inline bool isRealloc()     { return Info.Type & AllocInfo::Realloc; }
    inline bool isStrDup()      { return Info.Type & AllocInfo::StrDup; }
    inline bool isNew()         { return Info.Type & AllocInfo::New; }
    inline bool isFree()        { return Info.Type & AllocInfo::Free; }
    inline bool isDelete()      { return Info.Type & AllocInfo::Delete; }
    inline bool isHeapAlloc()   { return Info.Type & AllocInfo::HeapAlloc; }
    inline bool isStackAlloc()  { return Info.Type & AllocInfo::Alloca; }
    inline bool isGlobalAlloc() { return Info.Type & AllocInfo::Global; }
    inline bool isAnyAlloc()    { return Info.Type & AllocInfo::AnyAlloc; }
    inline bool isAnyFree()     { return Info.Type & AllocInfo::AnyFree; }
    inline bool isWrapper()     { return Info.IsWrapper; }

    Value *getCallParam(uint64_t i);

    uint64_t getConstSize();
    Value *getOrInsertSize(bool *Changed = nullptr);

    Value* getFreedPointer();
    Value* getRellocatedPointer();

    const SCEV *getSizeSCEV(ScalarEvolution &SE);
    const SCEV *getEndSCEV(ScalarEvolution &SE);
};

class AllocListIterator {
    typedef SmallVector<AllocSite*, 16> ListT;
    typedef typename ListT::iterator ListIt;
    typedef DenseMap<Function*, ListT> FuncMapT;
    typedef typename FuncMapT::iterator FuncMapIt;
    FuncMapIt FLI, FLE;
    ListT *L;
    ListIt LI;

public:
    typedef std::input_iterator_tag iterator_category;

    AllocListIterator(FuncMapIt B, FuncMapIt E)
        : FLI(B), FLE(E), L(&B->second), LI(L->begin()) {
        if (FLI != FLE)
            advanceToNextFunc();
    }

    AllocListIterator(AllocListIterator &I)
        : FLI(I.FLI), FLE(I.FLE), L(I.L), LI(I.LI) {}
    AllocListIterator(const AllocListIterator &I)
        : FLI(I.FLI), FLE(I.FLE), L(I.L), LI(I.LI) {}

    inline bool operator==(const AllocListIterator &y) const {
        assert(FLE == y.FLE && "uncomparable iterators");
        return FLI == y.FLI && (FLI == FLE || LI == y.LI);
    }
    inline bool operator!=(const AllocListIterator &y) const {
        return !operator==(y);
    }

    AllocListIterator& operator++() {
        ++LI;
        advanceToNextFunc();
        return *this;
    }
    inline AllocListIterator operator++(int) {
        AllocListIterator tmp = *this; ++*this; return tmp;
    }

    inline AllocSite& operator*()  const { return **LI; }
    inline AllocSite* operator->() const { return *LI; }
    //inline AllocSite* operator->() const { return &operator*(); }

    //inline bool atEnd() const { return FLI == FLE; }

private:
    void advanceToNextFunc() {
        while (LI == L->end()) {
            ++FLI;
            if (FLI == FLE)
                break;
            L = &FLI->second;
            LI = L->begin();
        }
    }

    friend class AllocsPass;
};

struct AllocsPass : ModulePass {
    typedef AllocListIterator::ListT SiteList;
    typedef AllocListIterator site_iterator;
    typedef iterator_range<site_iterator> site_range;

    static char ID;
    AllocsPass() : ModulePass(ID) {}

    bool runOnModule(Module &M) override;
    void getAnalysisUsage(AnalysisUsage &AU) const override;

private:
    DenseMap<Function*, SiteList> FuncSites;
    DenseMap<Value*, AllocSite*> SiteLookup;
    const DataLayout *DL;

    site_range func_sites(Function *F);

    bool isSafeAccess(Value *Addr, uint64_t TypeSize) const;

public:
    // on-demand functions
    AllocSite *getAllocSite(GlobalVariable &GV) { return getAllocSite(&GV); }
    AllocSite *getAllocSite(Instruction &I)     { return getAllocSite(&I); }
    AllocSite *getAllocSite(Value *V);

    // iterators for scanned all allocation sites (not on-demand)
    site_range sites();
    site_range global_sites()          { return func_sites(nullptr); }
    site_range func_sites(Function &F) { return func_sites(&F); }

    // basic static bound analysis for constant GEPs
    SizeOffsetType computeSizeAndOffset(Value *Addr);
    bool isInBoundsAccess(Value *Addr, uint64_t TypeSize);
    bool isInBounds(LoadInst &LI);
    bool isInBounds(StoreInst &SI);
    // TODO: AtomicRMWInst, AtomicCmpXchgInst, MemIntrinsic
    // TODO: make generic MemAccess.h utilities for all cases above (or -memaccess pass)
};

#endif /* !ALLOCS_H */
