#include <string>
#include <llvm/IR/DebugInfo.h>
#include <llvm/IR/InstIterator.h>
#include <llvm/IR/CallSite.h>
#include <llvm/ADT/SmallPtrSet.h>
#include <llvm/Support/raw_ostream.h>
#include "Utils/NoInstrument.h"

using namespace llvm;

static bool stripDebugInfoRecursive(Function &F, SmallPtrSetImpl<Function*> &Visited) {
    if (Visited.count(&F))
        return false;
    Visited.insert(&F);
    bool Changed = stripDebugInfo(F);
    if (Changed) {
        for (Instruction &I : instructions(F)) {
            CallSite CS(&I);
            if (CS && CS.getCalledFunction())
                stripDebugInfoRecursive(*CS.getCalledFunction(), Visited);
        }
    }
    return Changed;
}

static bool stripDebugInfoRecursive(Function &F) {
    SmallPtrSet<Function*, 4> Visited;
    return stripDebugInfoRecursive(F, Visited);
}

Function *createNoInstrumentFunction(Module &M, FunctionType *FnTy,
                                     StringRef Name, bool AlwaysInline) {
    std::string FullName(NOINSTRUMENT_PREFIX);
    FullName += Name;
    Function *F = Function::Create(FnTy, GlobalValue::InternalLinkage, FullName, &M);
    if (AlwaysInline)
        F->addFnAttr(Attribute::AlwaysInline);
    return F;
}

Function *getNoInstrumentFunction(Module &M, StringRef Name, bool AllowMissing) {
    std::string FullName(NOINSTRUMENT_PREFIX);
    FullName += Name;
    Function *F = M.getFunction(FullName);
    if (!F && !AllowMissing) {
        errs() << "Error: could not find helper function " << FullName << "\n";
        exit(1);
    }
    if (F)
        stripDebugInfoRecursive(*F);
    return F;
}

Function *getOrInsertNoInstrumentFunction(Module &M, StringRef Name, FunctionType *Ty) {
    std::string FullName(NOINSTRUMENT_PREFIX);
    FullName += Name;
    if (Function *F = M.getFunction(FullName)) {
        if (F->getFunctionType() != Ty) {
            errs() << "unexpected type for helper function " << FullName << "\n";
            errs() << "  expected: " << *Ty << "\n";
            errs() << "  found:    " << *F->getFunctionType() << "\n";
            exit(1);
        }
        stripDebugInfoRecursive(*F);
        return F;
    }
    return Function::Create(Ty, GlobalValue::ExternalLinkage, FullName, &M);
}

bool isNoInstrument(Value *V) {
    if (V && V->hasName()) {
        StringRef Name = V->getName();
        if (Name.startswith(NOINSTRUMENT_PREFIX))
            return true;
        // Support for mangled C++ names (should maybe do something smarter here)
        if (Name.startswith("_Z"))
            return Name.find(NOINSTRUMENT_PREFIX, 2) != StringRef::npos;
    }
    return false;
}

void setNoInstrument(Value *V) {
    V->setName(std::string(NOINSTRUMENT_PREFIX) + V->getName().str());
}

bool shouldInstrument(Function &F) {
    if (F.isDeclaration())
        return false;

    if (isNoInstrument(&F))
        return false;

    return true;
}
