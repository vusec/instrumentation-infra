#include <llvm/Pass.h>
#include <llvm/IR/Module.h>
#include <llvm/IR/Constants.h>
#include "llvm/IR/LegacyPassManager.h"
#include <llvm/Support/CommandLine.h>
#include <llvm/Support/Debug.h>
#include <llvm/Support/raw_ostream.h>
#include "llvm/Support/FileSystem.h"
#include <llvm/Transforms/IPO/PassManagerBuilder.h>

#define DEBUG_TYPE "dump-ir"

#include "Utils/Logging.h"
#include "Utils/NoInstrument.h"

#include <cstdlib>
#include <climits>
#include <fstream>
#include <cstring>

static const char *DISABLE_ENV_FLAG = "DISABLE_DUMP_IR";

using namespace llvm;

typedef std::map<unsigned, unsigned> uumap_t;

struct DumpIR : public ModulePass {
    static char ID;
    DumpIR() : ModulePass(ID) {}
    virtual bool runOnModule(Module &M);
    virtual void getAnalysisUsage(AnalysisUsage &AU) const {
        AU.setPreservesAll();
    }
};

char DumpIR::ID = 0;
static RegisterPass<DumpIR> X("dump-ir",
        "Generate .ll source file for current module");

#ifndef USE_GOLD_PASSES
static llvm::RegisterStandardPasses RegisterDumpIRLTO(
    llvm::PassManagerBuilder::EP_FullLinkTimeOptimizationEarly,
    [](const llvm::PassManagerBuilder &Builder,
       llvm::legacy::PassManagerBase &PM) { PM.add(new DumpIR()); });
#endif

#ifndef USE_GOLD_PASSES
static cl::opt<bool> ClDumpIR(
    "dump-ir",
    cl::desc("If set will be enabled"),
    cl::init(false));
#endif

static cl::opt<std::string> OutFile("dump-ir-to",
        cl::desc("Outfile for dumped llvm source"),
        cl::value_desc("path"));

static bool replaceSuffix(std::string &path, const char *oldExt, const char *newExt) {
    size_t pos = path.rfind(oldExt);
    if (pos != std::string::npos) {
        path.replace(pos, strlen(oldExt), newExt);
        return true;
    }
    return false;
}

static void saveModuleSource(Module &M, std::string path) {
    std::error_code error;
    raw_fd_ostream of(path.c_str(), error, sys::fs::F_None);
    if (error) {
        LOG_LINE("Error: could not open outfile " << path << ": " << error.message());
        exit(1);
    }
    of << M;
    of.close();
}

bool file_exists(const char *path) {
    std::ifstream file(path);
    return file.good();
}

StringRef getNameFromGlobal(Module &M) {
    if (GlobalVariable *GV = getNoInstrumentGlobal(M, "DEBUG_MODULE_NAME", true)) {
        if (!GV->hasInitializer()) {
            LOG_LINE("Warning: found DEBUG_MODULE_NAME without initializer");
        } else  {
            Constant *C = GV->getInitializer();
            ConstantDataSequential *CDS = cast<ConstantDataSequential>(C);
            return CDS->getAsCString();
        }
    }

    return StringRef();
}

bool DumpIR::runOnModule(Module &M) {
#ifndef USE_GOLD_PASSES
    if (!ClDumpIR) return false;
#endif
    char *envdisable = getenv(DISABLE_ENV_FLAG);
    if (envdisable && !strncmp(envdisable, "1", 2))
        return false;

    std::string path;

    if (OutFile.getNumOccurrences()) {
        path = OutFile;
    } else {
        StringRef ManualName = getNameFromGlobal(M);
        path = ManualName.empty() ? M.getModuleIdentifier() : ManualName.str();
        replaceSuffix(path, ".bc", "");
        replaceSuffix(path, ".c", "");
        replaceSuffix(path, ".cc", "");
        replaceSuffix(path, ".cpp", "");
        replaceSuffix(path, ".cxx", "");
        path += ".ll";
    }

    saveModuleSource(M, path);

    char *rp = realpath(path.c_str(), NULL);
    LOG_LINE("IR dumped in " << rp);
    free(rp);

    return false;
}
