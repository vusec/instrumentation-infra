#include <llvm/IR/Dominators.h>
#include <llvm/Analysis/LoopInfo.h>

#define DEBUG_TYPE "expand-const-global-users"

#include "utils/Common.h"

using namespace llvm;

struct ExpandConstGlobalUses : public ModulePass {
    static char ID;
    ExpandConstGlobalUses() : ModulePass(ID) {}

    bool runOnModule(Module &M) override;
};

char ExpandConstGlobalUses::ID = 0;
static RegisterPass<ExpandConstGlobalUses> X("expand-const-global-users",
        "Expand constantexprs of globals to instructions");

STATISTIC(NExpandedConsts, "Number of constant expressions expanded");
STATISTIC(NResultingInsts, "Number of instructions generated");

/// Determine the insertion point for this user. By default, insert immediately
/// before the user. SCEVExpander or LICM will hoist loop invariants out of the
/// loop. For PHI nodes, there may be multiple uses, so compute the nearest
/// common dominator for the incoming blocks.
static Instruction *getInsertPointForUses(Instruction *User, Value *Def,
                                          DominatorTree *DT) {
  PHINode *PHI = dyn_cast<PHINode>(User);
  if (!PHI)
    return User;

  Instruction *InsertPt = nullptr;
  for (unsigned i = 0, e = PHI->getNumIncomingValues(); i != e; ++i) {
    if (PHI->getIncomingValue(i) != Def) 
      continue;

    BasicBlock *InsertBB = PHI->getIncomingBlock(i);
    if (!InsertPt) {
      InsertPt = InsertBB->getTerminator();
      continue;
    }
    InsertBB = DT->findNearestCommonDominator(InsertPt->getParent(), InsertBB);
    InsertPt = InsertBB->getTerminator();
  }
  assert(InsertPt && "Missing phi operand");
  assert((!isa<Instruction>(Def) ||
          DT->dominates(cast<Instruction>(Def), InsertPt)) &&
         "def does not dominate all uses");
  return InsertPt;
}

static void findInstUsersOfConst(ConstantExpr *CE,
        SetVector<std::pair<Instruction*, ConstantExpr*>> &Insts) {
    for (User *U : CE->users()) {
        ifcast(ConstantExpr, UCE, U)
            findInstUsersOfConst(UCE, Insts);
        else ifcast(Instruction, I, U)
            Insts.insert(std::make_pair(I, CE));
        else
            assert(isa<Constant>(U));
    }
}

static void expandConstOperandOfInst(Instruction *I, ConstantExpr *CE, Instruction *InsertBefore) {
    Instruction *UI = CE->getAsInstruction();
    UI->insertBefore(InsertBefore);
    I->replaceUsesOfWith(CE, UI);
    ++NResultingInsts;
    for (Use &UU : UI->operands()) {
        ifcast(ConstantExpr, OCE, UU.get())
            expandConstOperandOfInst(UI, OCE, UI);
    }
}

bool ExpandConstGlobalUses::runOnModule(Module &M) {
    SetVector<std::pair<Instruction*, ConstantExpr*>> ExpandInsts;

    for (GlobalVariable &GV : M.globals()) {
        if (GV.getName().startswith("llvm."))
            continue;

        if (isNoInstrument(&GV))
            continue;

        for (User *U : GV.users()) {
            ifcast(ConstantExpr, CE, U)
                findInstUsersOfConst(CE, ExpandInsts);
        }
    }

    DenseMap<Function*, DominatorTree*> DTMap;

    for (auto P : ExpandInsts) {
        Instruction *I = P.first;
        ConstantExpr *CE = P.second;

        // Skip exception handling pads since we cannot insert instructions
        // there (and we don't want to tag pointers there anyways)
        if (I->isEHPad())
            continue;

        // Skip intrinsics like @llvm.eh.typeid.for that are evaluated at
        // compile time and need an actual global pointer for type inspection
        ifcast(CallInst, CI, I) {
            Function *F = CI->getCalledFunction();
            if (F && F->isIntrinsic()) {
                switch (F->getIntrinsicID()) {
                    case Intrinsic::dbg_declare:
                    case Intrinsic::dbg_value:
                    case Intrinsic::lifetime_start:
                    case Intrinsic::lifetime_end:
                    case Intrinsic::invariant_start:
                    case Intrinsic::invariant_end:
                    case Intrinsic::eh_typeid_for:
                    case Intrinsic::eh_return_i32:
                    case Intrinsic::eh_return_i64:
                        continue;
                    default:
                        break;
                }
            }
        }

        Function *F = I->getParent()->getParent();

        if (!shouldInstrument(F))
            continue;

        // Skip static global constructors which would store tagged global
        // pointers in objects which are later dereferenced by free()
        if (F->hasName() && F->getName().startswith("_GLOBAL__sub_I_"))
            continue;

        DominatorTree *DT;
        auto It = DTMap.find(F);
        if (It == DTMap.end()) {
            DT = new DominatorTree(*F);
            DTMap.insert(std::make_pair(F, DT));
        } else {
            DT = It->second;
        }

        expandConstOperandOfInst(I, CE, getInsertPointForUses(I, CE, DT));
        ++NExpandedConsts;
    }

    for (auto I = DTMap.begin(), E = DTMap.end(); I != E; ++I)
        delete I->second;

    for (GlobalVariable &GV : M.globals())
        GV.removeDeadConstantUsers();

    return !ExpandInsts.empty();
}
