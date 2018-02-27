#include <llvm/Transforms/Utils/ModuleUtils.h>

#define DEBUG_TYPE "defer-global-init"

#include "utils/Common.h"

using namespace llvm;

struct DeferGlobalInit : public ModulePass {
    static char ID;
    DeferGlobalInit() : ModulePass(ID) {}

    bool runOnModule(Module &M) override;

private:
    typedef SmallVector<unsigned, 4> IndexList;
    typedef struct {
        GlobalVariable *GV;
        Constant *V;
        IndexList Indices;
    } ReplacementEntry;

    SmallVector<ReplacementEntry, 10> Replaced;

    Constant *extractGlobals(Constant *C, GlobalVariable *GV, IndexList &Indices);
};

char DeferGlobalInit::ID = 0;
static RegisterPass<DeferGlobalInit> X("defer-global-init",
        "Replace globals in initializers with nullptrs and do the "
        "initialization in a constructor instead");

STATISTIC(NReplaced, "Number of global initializers moved to constructor");

static bool containsGlobal(Constant *C) {
    if (isa<GlobalVariable>(C))
        return true;

    for (Use &U : C->operands()) {
        if (containsGlobal(cast<Constant>(U.get())))
            return true;
    }

    return false;
}

Constant *DeferGlobalInit::extractGlobals(Constant *C, GlobalVariable *GV,
                                          IndexList &Indices) {
    if (isa<GlobalVariable>(C) || (isa<ConstantExpr>(C) && containsGlobal(C))) {
        ReplacementEntry Entry = {GV, C, Indices};
        Replaced.push_back(Entry);
        ++NReplaced;
        return ConstantPointerNull::get(cast<PointerType>(C->getType()));
    }

    if (isa<ConstantStruct>(C) ||
        isa<ConstantArray>(C) ||
        isa<ConstantVector>(C)) {
        SmallVector<Constant*, 8> Ops;
        for (Use &O : C->operands()) {
            Indices.push_back(O.getOperandNo());
            assert(isa<Constant>(O.get()));
            Ops.push_back(extractGlobals(cast<Constant>(O.get()), GV, Indices));
            Indices.pop_back();
        }

        ifcast(ConstantStruct, Struct, C)
            return ConstantStruct::get(Struct->getType(), Ops);
        else ifcast(ConstantArray, Array, C)
            return ConstantArray::get(Array->getType(), Ops);
        else
            return ConstantVector::get(Ops);
    }

    return C;
}

bool DeferGlobalInit::runOnModule(Module &M) {
    for (GlobalVariable &GV : M.globals()) {
        if (GV.getName().startswith("llvm."))
            continue;

        if (GV.hasInitializer()) {
            Constant *Old = GV.getInitializer();
            IndexList Indices;
            Constant *New = extractGlobals(Old, &GV, Indices);
            if (New != Old) {
                GV.setInitializer(New);
                GV.setConstant(false);
            }
        }
    }

    FunctionType *FnTy = FunctionType::get(Type::getVoidTy(M.getContext()), false);
    Function *F = Function::Create(FnTy, GlobalValue::InternalLinkage, ".initialize_globals", &M);
    IRBuilder<> B(BasicBlock::Create(F->getContext(), "entry", F));

    for (ReplacementEntry &E : Replaced) {
        Value *Slot = E.GV;
        if (E.Indices.size()) {
            SmallVector<Value*, 5> IdxList = { B.getInt32(0) };
            IdxList.reserve(E.Indices.size() + 1);
            for (unsigned Index : E.Indices)
                IdxList.push_back(B.getInt32(Index));
            Slot = B.CreateInBoundsGEP(E.GV, IdxList);
        }
        B.CreateStore(E.V, Slot);
    }

    B.CreateRetVoid();
    appendToGlobalCtors(M, F, -2);

    bool Changed = !Replaced.empty();
    Replaced.clear();
    return Changed;
}
