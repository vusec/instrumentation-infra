#ifndef BUILTIN_CUSTOM_FUNCTION_PASS_H
#define BUILTIN_CUSTOM_FUNCTION_PASS_H

#include <llvm/Pass.h>
#include <llvm/IR/Module.h>
#include <llvm/IR/Function.h>
#include "NoInstrument.h"

using namespace llvm;

struct ModuleFunctionPass : public ModulePass {
    ModuleFunctionPass(char &ID) : ModulePass(ID) {}
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

#endif // BUILTIN_CUSTOM_FUNCTION_PASS_H
