#ifndef BUILTIN_NO_INSTRUMENT_H
#define BUILTIN_NO_INSTRUMENT_H

#include <llvm/IR/Value.h>
#include <llvm/IR/Module.h>
#include <llvm/IR/Function.h>
#include <llvm/ADT/StringRef.h>

#define NOINSTRUMENT_PREFIX "__noinstrument_"

bool isNoInstrument(llvm::Value *V);

void setNoInstrument(llvm::Value *V);

bool shouldInstrument(llvm::Function &F);

static inline bool shouldInstrument(llvm::Function *F) {
    assert(F);
    return !shouldInstrument(*F);
}

llvm::Function* createNoInstrumentFunction(llvm::Module &M,
                                           llvm::FunctionType *FnTy,
                                           llvm::StringRef Name,
                                           bool AlwaysInline=true);

llvm::Function* getNoInstrumentFunction(llvm::Module &M,
                                        llvm::StringRef Name,
                                        bool AllowMissing=false);

llvm::Function* getOrInsertNoInstrumentFunction(llvm::Module &M,
                                                llvm::StringRef Name,
                                                llvm::FunctionType *Ty);

#endif // BUILTIN_NO_INSTRUMENT_H
