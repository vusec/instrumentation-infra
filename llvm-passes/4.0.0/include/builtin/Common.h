#ifndef BUILTIN_COMMON_H
#define BUILTIN_COMMON_H

#include <llvm/Pass.h>
#include <llvm/IR/Module.h>
#include <llvm/IR/Function.h>
#include <llvm/IR/Instruction.h>
#include <llvm/IR/Instructions.h>
#include <llvm/IR/IntrinsicInst.h>
#include <llvm/IR/InstIterator.h>
#include <llvm/IR/Intrinsics.h>
#include <llvm/IR/Constant.h>
#include <llvm/IR/Constants.h>
#include <llvm/IR/IRBuilder.h>
#include <llvm/IR/CallSite.h>
#include <llvm/IR/CFG.h>
#include <llvm/Analysis/ScalarEvolution.h>
#include <llvm/Analysis/ScalarEvolutionExpressions.h>
#include <llvm/Support/CommandLine.h>
#include <llvm/Support/raw_ostream.h>
#include <llvm/Support/Debug.h>
#include <llvm/ADT/SmallVector.h>
#include <llvm/ADT/SmallSet.h>
#include <llvm/ADT/DenseMap.h>
#include <llvm/ADT/DenseSet.h>
#include <llvm/ADT/MapVector.h>
#include <llvm/ADT/SetVector.h>
#include <llvm/ADT/Statistic.h>

#include <string>
#include <list>
#include <set>
#include <vector>
#include <cassert>
#include <sstream>

#include "Utils/NoInstrument.h"
#include "Utils/Casting.h"
#include "Utils/Logging.h"

using namespace llvm;

enum Possibility { No, Yes, Maybe };

Instruction *getInsertPointAfter(Instruction *I);
Instruction *getInsertPointAfter(Argument *I);

void collectPHIOrigins(PHINode *PN, std::vector<Value*> &Origins);
void collectPHIUsers(PHINode *PN, SetVector<User*> &Uses);
void collectUsersThroughPHINodes(User *U, SetVector<User*> &Uses);

inline std::vector<Value*> PHIOrigins(PHINode *PN) {
    std::vector<Value*> Origins;
    collectPHIOrigins(PN, Origins);
    return Origins;
}

inline SetVector<User*> PHIUsers(PHINode *PN) {
    SetVector<User*> Users;
    collectPHIUsers(PN, Users);
    return Users;
}

SmallVector<std::pair<Value*, User*>, 4> usersThroughPHINodes(Value *V);

inline Value* otherOperand(Instruction *I, Value *Op) {
    assert(I->getNumOperands() == 2);

    if (I->getOperand(0) == Op)
        return I->getOperand(1);

    assert(I->getOperand(1) == Op);
    return I->getOperand(0);
}

inline int getOperandNo(User *U, Value *Op, bool AllowMissing=false) {
    for (Use &UU : U->operands()) {
        if (UU.get() == Op)
            return (int)UU.getOperandNo();
    }
    assert(AllowMissing);
    return -1;
}

inline bool isUnionType(Type *Ty) {
    return Ty->isStructTy() && Ty->getStructName().startswith("union.");
}

inline std::string hex(uint64_t i) {
    std::stringstream ss;
    ss << std::hex << i;
    return ss.str();
}

inline std::string padr(std::string s, size_t width) {
    return s.size() >= width ? s : s + std::string(width - s.size(), ' ');
}

inline std::string padl(std::string s, size_t width) {
    return s.size() >= width ? s : std::string(width - s.size(), ' ') + s;
}

Argument *getFunctionArgument(Function *F, unsigned Idx);

template<typename T = User>
inline T *getSingleUser(Value *V) {
    assert(V->getNumUses() == 1);
    return cast<T>(*V->user_begin());
}

#endif // BUILTIN_COMMON_H
