#ifndef CUSTOM_FUNCTION_PASS_H
#define CUSTOM_FUNCTION_PASS_H

#include "Common.h"

using namespace llvm;

static bool shouldInstrument(Function &F) {
    if (F.isDeclaration())
        return false;

    if (isNoInstrument(&F))
        return false;

    return true;
}

struct CustomFunctionPass : public ModulePass {
    CustomFunctionPass(char &ID) : ModulePass(ID) {}
    bool runOnModule(Module &M) override {
        bool Changed = initializeModule(M);

        for (Function &F : M) {
            if (shouldInstrument(F))
                Changed |= runOnFunction(F);
        }

        Changed |= finalizeModule(M);

        return Changed;
    }

protected:
    virtual bool initializeModule(Module &M) { return false; }
    virtual bool runOnFunction(Function &F) = 0;
    virtual bool finalizeModule(Module &M) { return false; }
};

#endif /* !CUSTOM_FUNCTION_PASS_H */
