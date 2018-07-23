#include "Common.h"

using namespace llvm;

/*
 * Get the insert point after the specified instruction. For non-terminators
 * this is the next instruction. For `invoke` intructions, create a new
 * fallthrough block that jumps to the default return target, and return the
 * jump instruction.
 */
Instruction *getInsertPointAfter(Instruction *I) {
    if (InvokeInst *Invoke = dyn_cast<InvokeInst>(I)) {
        BasicBlock *Dst = Invoke->getNormalDest();
        BasicBlock *NewBlock = BasicBlock::Create(I->getContext(),
                "invoke_insert_point", Dst->getParent(), Dst);
        BranchInst *Br = BranchInst::Create(Dst, NewBlock);
        Invoke->setNormalDest(NewBlock);

        /* Patch references in PN nodes in original successor */
        BasicBlock::iterator It(Dst->begin());
        while (PHINode *PN = dyn_cast<PHINode>(It)) {
            int i;
            while ((i = PN->getBasicBlockIndex(Invoke->getParent())) >= 0)
                PN->setIncomingBlock(i, NewBlock);
            It++;
        }

        return Br;
    }

    if (isa<PHINode>(I))
        return &*I->getParent()->getFirstInsertionPt();

    assert(!isa<TerminatorInst>(I));
    return &*std::next(BasicBlock::iterator(I));
}

/*
 * For function arguments, the insert point is in the entry basic block.
 */
Instruction *getInsertPointAfter(Argument *A) {
    Function *F = A->getParent();
    assert(!F->empty());
    return &*F->getEntryBlock().getFirstInsertionPt();
}

static void collectPHIOriginsRecursive(PHINode *PN,
        std::vector<Value*> &Origins,
        std::set<Value*> &Visited) {
    for (unsigned I = 0, E = PN->getNumIncomingValues(); I < E; ++I) {
        Value *V = PN->getIncomingValue(I);

        if (Visited.count(V) != 0)
            continue;
        Visited.insert(V);

        ifcast(PHINode, IPN, V)
            collectPHIOriginsRecursive(IPN, Origins, Visited);
        else
            Origins.push_back(V);
    }
}

void collectPHIOrigins(PHINode *PN, std::vector<Value*> &Origins) {
    std::set<Value*> Visited = {PN};
    collectPHIOriginsRecursive(PN, Origins, Visited);
}

static void collectPHIUsersRecursive(PHINode *PN,
        SetVector<User*> &Users,
        SmallSet<PHINode*, 4> &Visited) {
    for (User *U : PN->users()) {
        ifcast(PHINode, UPN, U) {
            if (Visited.count(UPN) == 0) {
                Visited.insert(UPN);
                collectPHIUsersRecursive(UPN, Users, Visited);
            }
        } else {
            Users.insert(U);
        }
    }
}

void collectPHIUsers(PHINode *PN, SetVector<User*> &Users) {
    SmallSet<PHINode*, 4> Visited;
    Visited.insert(PN);
    collectPHIUsersRecursive(PN, Users, Visited);
}

void collectUsersThroughPHINodes(Value *V, SetVector<User*> &Users) {
    SmallSet<PHINode*, 4> Visited;
    for (User *UU : V->users()) {
        ifcast(PHINode, PN, UU) {
            Visited.clear();
            Visited.insert(PN);
            collectPHIUsersRecursive(PN, Users, Visited);
        } else {
            Users.insert(UU);
        }
    }
}

static void collectPHIUsersRecursive(PHINode *PN,
        SmallVectorImpl<std::pair<Value*, User*>> &Users,
        SmallSet<PHINode*, 4> &Visited) {
    for (User *U : PN->users()) {
        ifcast(PHINode, UPN, U) {
            if (Visited.count(UPN) == 0) {
                Visited.insert(UPN);
                collectPHIUsersRecursive(UPN, Users, Visited);
            }
        } else {
            Users.push_back(std::make_pair(PN, U));
        }
    }
}

SmallVector<std::pair<Value*, User*>, 4> usersThroughPHINodes(Value *V) {
    SmallVector<std::pair<Value*, User*>, 4> Users;
    SmallSet<PHINode*, 4> Visited;

    for (User *UU : V->users()) {
        ifcast(PHINode, PN, UU) {
            Visited.clear();
            Visited.insert(PN);
            collectPHIUsersRecursive(PN, Users, Visited);
        } else {
            Users.push_back(std::make_pair(V, UU));
        }
    }

    return std::move(Users);
}

Argument *getFunctionArgument(Function *F, unsigned Idx) {
    unsigned i = 0;
    for (Argument &Arg : F->args()) {
        if (i++ == Idx)
            return &Arg;
    }
    return nullptr;
}

