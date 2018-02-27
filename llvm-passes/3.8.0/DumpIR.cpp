#include <llvm/Pass.h>
#include <llvm/IR/Module.h>
#include <llvm/IR/Constants.h>
#include <llvm/Support/CommandLine.h>
#include <llvm/Support/Debug.h>
#include <llvm/Support/raw_ostream.h>
#include "llvm/Support/FileSystem.h"

#define DEBUG_TYPE "dump-ir"

#include "utils/Common.h"

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
    for (GlobalVariable &GV : M.globals()) {
        if (GV.getName() != NOINSTRUMENT_PREFIX "DEBUG_MODULE_NAME")
            continue;

        if (!GV.hasInitializer()) {
            LOG_LINE("Warning: found DEBUG_MODULE_NAME without initializer");
            continue;
        }

        Constant *C = GV.getInitializer();
        ConstantDataSequential *CDS = cast<ConstantDataSequential>(C);
        return CDS->getAsCString();
    }
    return StringRef();
}

bool DumpIR::runOnModule(Module &M) {
    char *envdisable = getenv(DISABLE_ENV_FLAG);
    if (envdisable && !strncmp(envdisable, "1", 2))
        return false;

    std::string path;

    if (OutFile.getNumOccurrences()) {
        path = OutFile;
    } else {
        StringRef ManualModName = getNameFromGlobal(M);
        if (!ManualModName.empty()) {
            path = ManualModName.str();
        } else {
            path = M.getModuleIdentifier();
            replaceSuffix(path, ".c", "");
            replaceSuffix(path, ".bc", "");

#if 0
            // XXX hack to not overwrite file (for sizetagstest now)
            std::string suffix = "";
            int cnt = 0;
            while (file_exists((path + suffix + ".ll").c_str())) {
                cnt++;
                suffix = "-" + std::to_string(cnt);
            }
            path += suffix;
#endif
        }

        path += ".ll";
    }

    saveModuleSource(M, path);

    char *rp = realpath(path.c_str(), NULL);
    LOG_LINE("IR dumped in " << rp);
    free(rp);

    return false;
}
