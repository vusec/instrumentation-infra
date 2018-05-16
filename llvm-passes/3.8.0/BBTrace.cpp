#include "Common.h"
#include "CustomFunctionPass.h"

#define DEBUG_TYPE "bbtrace"

using namespace llvm;

struct BBTrace : public CustomFunctionPass {
    static char ID;
    BBTrace() : CustomFunctionPass(ID) {}

private:
    Function *TraceFunc;
    unsigned long long NBBs;

    bool initializeModule(Module &M) override;
    bool runOnFunction(Function &F) override;
};

char BBTrace::ID = 0;
static RegisterPass<BBTrace> X("bbtrace",
        "Log a unique number at the start of each basic block (for trace comparison)");

bool BBTrace::initializeModule(Module &M) {
    TraceFunc = getNoInstrumentFunction(M, "trace_bb");
    NBBs = 0;
    return false;
}

bool BBTrace::runOnFunction(Function &F) {
    for (BasicBlock &BB : F) {
        IRBuilder<> B(&*BB.getFirstInsertionPt());
        B.CreateCall(TraceFunc, B.getInt32(++NBBs));
    }

    return true;
}
