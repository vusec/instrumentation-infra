#ifndef ALLOCATION_UTILS_H
#define ALLOCATION_UTILS_H

#include <llvm/IR/Instruction.h>
#include <llvm/IR/Instructions.h>
#include <llvm/IR/IRBuilder.h>
#include <llvm/IR/DataLayout.h>
#include <llvm/Analysis/ScalarEvolution.h>

using namespace llvm;

struct AllocationSite {
    enum AllocationType { Malloc, Calloc, Realloc, Alloca };
    static const size_t NoSize = (size_t)(-1LL);

    Instruction *Allocation;
    AllocationType CallType;
    int SizeArg;
    bool IsWrapped;

    inline bool isStackAllocation() { return CallType == Alloca; }
    inline bool isHeapAllocation() { return CallType != Alloca; }

    Value *instrumentWithByteSize(IRBuilder<> &B, const DataLayout &DL);
    size_t getConstSize(const DataLayout &DL);
    const SCEV *getSizeSCEV(ScalarEvolution &SE);
    const SCEV *getEndPointerSCEV(ScalarEvolution &SE);
};

bool isMalloc(Function *F);
bool isCalloc(Function *F);
bool isRealloc(Function *F);
bool isFree(Function *F);
bool isMallocWrapper(Function *F);
bool isCallocWrapper(Function *F);
bool isReallocWrapper(Function *F);
bool isFreeWrapper(Function *F);
bool isAllocationFunc(Function *F);
bool isFreeFunc(Function *F);

int getSizeArg(Function *F);

bool isAllocation(Instruction *I, AllocationSite &AI);

const SCEV *getGlobalSizeSCEV(GlobalVariable *GV, ScalarEvolution &SE);

#endif /* !ALLOCATION_UTILS_H */
