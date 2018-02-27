#ifndef SIZE_OF_TYPES_H
#define SIZE_OF_TYPES_H

#include "llvm/Pass.h"
#include "llvm/ADT/DenseMap.h"

using namespace llvm;

struct SizeofTypes : public ModulePass {
    static char ID;
    SizeofTypes() : ModulePass(ID) {}

    virtual void getAnalysisUsage(AnalysisUsage &AU) const;
    virtual bool runOnModule(Module &M);

    Type *getSizeofType(Instruction *I);
    void setSizeofType(Instruction *I, Type *Ty);

private:
    DenseMap<Instruction*, Type*> mallocTypes;
};

#endif  /* SIZE_OF_TYPES_H */
