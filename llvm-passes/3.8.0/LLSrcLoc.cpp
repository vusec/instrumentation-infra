#include <llvm/Pass.h>
#include <llvm/IR/Module.h>
#include <llvm/IR/Function.h>
#include <llvm/IR/Instruction.h>
#include <llvm/IR/Instructions.h>
//#include <llvm/IR/IntrinsicInst.h>
//#include <llvm/IR/InstIterator.h>
//#include <llvm/IR/Intrinsics.h>
#include <llvm/IR/Constant.h>
#include <llvm/IR/Constants.h>
//#include <llvm/IR/IRBuilder.h>
#include <llvm/IR/DIBuilder.h>
#include <llvm/Support/CommandLine.h>
#include <llvm/Support/Debug.h>
#include <llvm/Support/raw_ostream.h>
#include "llvm/Support/FileSystem.h"
#include "llvm/Transforms/Utils/Cloning.h"

#include <cstdio>
#include <map>
#include <fstream>
#include <string>
#include <cstring>
#include <cassert>
#include <libgen.h>
#include <unistd.h>
#include <limits.h>

#define DEBUG_TYPE "ll-srcloc"
#define MDNAME "myline"
#define MDID "ll"

using namespace llvm;

typedef std::map<unsigned, unsigned> uumap_t;

struct LLSrcLoc : public ModulePass {
    static char ID;
    LLSrcLoc() : ModulePass(ID) {}
    virtual bool runOnModule(Module &M);
};

char LLSrcLoc::ID = 0;
static RegisterPass<LLSrcLoc> X("ll-srcloc",
        "Generate .ll source file and add DWARF debug symbols referring to that source file");

static cl::opt<std::string> OutFile("ll-outfile",
        cl::desc("Single outfile for llvm source for DWARF debug info"),
        cl::value_desc("path"));

static void setInstIDs(Module &M) {
    LLVMContext &ctx = M.getContext();
    unsigned uniqval = 1;

    for (Function &F : M) {
        if (!F.isDeclaration())
            F.setMetadata(MDNAME, MDTuple::get(ctx, MDString::get(ctx, MDID + std::to_string(uniqval++))));

        for (BasicBlock &BB : F) {
            for (Instruction &I : BB)
                I.setMetadata(MDNAME, MDTuple::get(ctx, MDString::get(ctx, MDID + std::to_string(uniqval++))));
        }
    }
}

static void saveModuleSource(Module &M, std::string path) {
    std::error_code error;
    raw_fd_ostream of(path.c_str(), error, sys::fs::F_None);
    if (error) {
        errs() << "Error: could not open outfile " << path << ": " << error.message() << "\n";
        exit(1);
    }
    of << M;
    of.close();
}

static void collectLineNumbers(StringRef path, uumap_t &idmap) {
    std::ifstream infile(path);
    assert(infile);

    std::string line;
    unsigned lineno = 1;
    uumap_t mdmap;

    while (std::getline(infile, line)) {
        size_t pos = line.find("!" MDNAME " !");
        unsigned md, id;

        if (pos != std::string::npos) {
            const char *ptr = line.c_str() + pos + strlen("!" MDNAME " !");

            if (sscanf(ptr, "%u", &md) != 1) {
                errs() << "Error: could not read " MDNAME " index in line: " << line;
                exit(1);
            }

            mdmap[md] = lineno;
        }
        else if (sscanf(line.c_str(), "!%u = !{!\"" MDID "%u\"}", &md, &id) == 2) {
            auto it = mdmap.find(md);

            if (it != mdmap.end())
                idmap[id] = it->second;
        }

        lineno++;
    }
}

static inline NamedMDNode *clearNamedMetadata(Module &M, StringRef name) {
    M.getOrInsertNamedMetadata(name)->eraseFromParent();
    return M.getOrInsertNamedMetadata(name);
}

static unsigned lineNoFromMDNode(MDNode *md, uumap_t &idmap) {
    StringRef idstr = cast<MDString>(md->getOperand(0))->getString();
    int64_t idbuf;
    idstr.substr(sizeof MDID - 1).getAsInteger(10, idbuf);
    unsigned id = static_cast<unsigned>(idbuf);
    uumap_t::iterator it = idmap.find(id);
    assert(it != idmap.end());
    return it->second;
}

static void replaceIDsWithLineNumbers(Module &M, uumap_t &idmap, const std::string &filename) {
    LLVMContext &ctx = M.getContext();
    DIBuilder DBuilder(M);
    std::string directory;
    char dirbuf[PATH_MAX];

    if (getcwd(dirbuf, sizeof dirbuf) == nullptr) {
        perror("getcwd");
        exit(1);
    }
    directory = std::string(dirbuf);
    DEBUG(dbgs() << "Directory is: " << directory << " \n");

    clearNamedMetadata(M, "llvm.dbg.cu")->addOperand(
            DBuilder.createCompileUnit(dwarf::DW_LANG_C99, filename,
            //DBuilder.createCompileUnit(dwarf::DW_LANG_C, filename,
                directory, "ll-srcloc pass", false, "", 0));

    DIFile *fileScope = DBuilder.createFile(filename, directory);

    for (Function &F : M) {
        MDNode *md = F.getMetadata(MDNAME);
        if (!md)
            continue;

        unsigned lineno = lineNoFromMDNode(md, idmap);
        DISubprogram *scope = DBuilder.createFunction(
                fileScope, F.getName(), "", fileScope, lineno,
                DBuilder.createSubroutineType(DBuilder.getOrCreateTypeArray(None)),
                GlobalValue::isLocalLinkage(F.getLinkage()),
                !F.isDeclaration(), lineno);
        F.setSubprogram(scope); // XXX: test
        // TODO: add a dummy stackframe based on alloca's or use existing -g
        // info and see if this makes gdb print the line correctly

        F.setMetadata(MDNAME, nullptr);

        for (BasicBlock &BB : F) {
            for (Instruction &I : BB) {
                if (MDNode *md = I.getMetadata(MDNAME)) {
                    unsigned lineno = lineNoFromMDNode(md, idmap);
                    I.setDebugLoc(DebugLoc(DILocation::get(ctx, lineno, 3, scope)));
                    I.setMetadata(MDNAME, nullptr);
                }
            }
        }
    }

    DBuilder.finalize();
}

static bool replaceSuffix(std::string &path, const char *oldExt, const char *newExt) {
    size_t pos = path.rfind(oldExt);
    if (pos != std::string::npos) {
        path.replace(pos, strlen(oldExt), newExt);
        return true;
    }
    return false;
}

static void setGlobalDebugInfo(Module &M) {
    // FIXME: should get versions dynamically
    // FIXME: this breaks on some systems
    M.addModuleFlag(Module::Warning, "Dwarf Version", 4);
    M.addModuleFlag(Module::Warning, "Debug Info Version", 3);

    LLVMContext &ctx = M.getContext();
    clearNamedMetadata(M, "llvm.ident")->addOperand(
            MDTuple::get(ctx, MDString::get(ctx, "ll-srcloc pass")));
}

bool LLSrcLoc::runOnModule(Module &M) {
    uumap_t idmap;
    std::string path;

    if (OutFile.getNumOccurrences()) {
        path = OutFile;
    } else {
        path = M.getModuleIdentifier();
        replaceSuffix(path, ".bc", "");
        replaceSuffix(path, ".ll", "");
        path += ".dbg.ll";
    }

    DEBUG(dbgs() << "Using outfile: " << path << "\n");

    StripDebugInfo(M);
    setInstIDs(M);
    setGlobalDebugInfo(M);
    saveModuleSource(M, path);
    collectLineNumbers(path, idmap);
    //StripDebugInfo(M);
    replaceIDsWithLineNumbers(M, idmap, path);

    std::unique_ptr<Module> strippedCopy = CloneModule(&M);
    assert(strippedCopy);
    // TODO: strip !myline metadata
    //StripDebugInfo(*strippedCopy);
    saveModuleSource(*strippedCopy, path);

    return true;
}
