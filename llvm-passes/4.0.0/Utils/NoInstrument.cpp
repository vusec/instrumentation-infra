#include <string>
#include <llvm/IR/DebugInfo.h>
#include <llvm/Support/raw_ostream.h>
#include "Utils/NoInstrument.h"

using namespace llvm;

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
        stripDebugInfo(*F);
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
        stripDebugInfo(*F);
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
