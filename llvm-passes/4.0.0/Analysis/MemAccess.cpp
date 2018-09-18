#include <llvm/IR/Module.h>
#include <llvm/IR/InstIterator.h>
#include <llvm/IR/DataLayout.h>
#include <llvm/IR/CallSite.h>
#include <llvm/Support/raw_ostream.h>
#include "Analysis/MemAccess.h"

using namespace llvm;

static CallInst *getMemCmp(Instruction &I) {
    if (CallInst *CI = dyn_cast<CallInst>(&I)) {
        Function *F = CI->getCalledFunction();
        if (F && F->hasName() && F->getName() == "memcmp")
            return CI;
    }
    return nullptr;
}

void MemAccess::setPointer(Value *P) {
    if (LoadInst *LI = dyn_cast<LoadInst>(I)) {
        LI->setOperand(LI->getPointerOperandIndex(), P);
    } else if (StoreInst *SI = dyn_cast<StoreInst>(I)) {
        SI->setOperand(SI->getPointerOperandIndex(), P);
    } else if (AtomicCmpXchgInst *CX = dyn_cast<AtomicCmpXchgInst>(I)) {
        CX->setOperand(CX->getPointerOperandIndex(), P);
    } else if (AtomicRMWInst *RMW = dyn_cast<AtomicRMWInst>(I)) {
        RMW->setOperand(RMW->getPointerOperandIndex(), P);
    } else if (MemTransferInst *MT = dyn_cast<MemTransferInst>(I)) {
        if (IsRead)
            MT->setSource(P);
        else
            MT->setDest(P);
    } else if (MemIntrinsic *MI = dyn_cast<MemIntrinsic>(I)) {
        MI->setDest(P);
    } else if (CallInst *CI = getMemCmp(*I)) {
        assert(Pointer == CI->getArgOperand(0) || Pointer == CI->getArgOperand(1));
        CI->setOperand(Pointer == CI->getArgOperand(0) ? 0 : 1, P);
    } else {
        assert(!"invalid instruction");
    }
    Pointer = P;
}

void MemAccess::setValue(Value *V) {
    assert(isWrite() && "can only set value of writes");
    if (StoreInst *SI = dyn_cast<StoreInst>(I)) {
        SI->setOperand(0, V);
    } else if (AtomicCmpXchgInst *CX = dyn_cast<AtomicCmpXchgInst>(I)) {
        CX->setOperand(2, V);
    } else if (AtomicRMWInst *RMW = dyn_cast<AtomicRMWInst>(I)) {
        RMW->setOperand(1, V);
    } else if (MemSetInst *MS = dyn_cast<MemSetInst>(I)) {
        MS->setValue(V);
    } else {
        assert(!"invalid instruction");
    }
}

static std::string stripIndent(const Value *V) {
    std::string buf;
    raw_string_ostream ss(buf);
    ss << *V;
    ss.flush();
    if (buf.size() >= 2 && buf[0] == ' ' && buf[1] == ' ')
        return buf.substr(2);
    return buf;
}

void MemAccess::print(raw_ostream &O) const {
    O << "Mem" << (IsRead ? "Read(" : "Write(");
    O << "inst={ " << stripIndent(I) << " }";
    if (isa<ConstantInt>(Length))
        O << " length=" << getConstLength();
    else
        O << " length={ " << stripIndent(Length) << " }";
    if (Alignment)
        O << " align=" << Alignment;
    O << ")";
}

static inline Value *getSize(const Instruction &I, const Value *TypeVal) {
    const DataLayout &DL = I.getModule()->getDataLayout();
    unsigned NBytes = DL.getTypeStoreSize(TypeVal->getType());
    return ConstantInt::get(DL.getLargestLegalIntType(I.getContext()), NBytes);
}

static inline unsigned getAlign(const Instruction &I, const Value *V) {
    const DataLayout &DL = I.getModule()->getDataLayout();
    return V->getPointerAlignment(DL);
}

unsigned MemAccess::get(Instruction &I, SmallVectorImpl<MemAccess> &MA) {
    return MemRead::get(I, MA) + MemWrite::get(I, MA);
}

unsigned MemRead::get(Instruction &I, SmallVectorImpl<MemAccess> &MA) {
    if (LoadInst *LI = dyn_cast<LoadInst>(&I)) {
        MA.emplace_back(I, LI->getPointerOperand(), getSize(I, LI), LI->getAlignment(), true);
        return 1;
    }

    if (MemTransferInst *MT = dyn_cast<MemTransferInst>(&I)) {
        MA.emplace_back(I, MT->getRawSource(), MT->getLength(), MT->getAlignment(), true);
        return 1;
    }

    if (AtomicCmpXchgInst *CX = dyn_cast<AtomicCmpXchgInst>(&I)) {
        Value *Len = getSize(I, CX->getCompareOperand());
        unsigned Align = getAlign(I, CX->getPointerOperand());
        MA.emplace_back(I, CX->getPointerOperand(), Len, Align, true);
        return 1;
    }

    if (AtomicRMWInst *RMW = dyn_cast<AtomicRMWInst>(&I)) {
        Value *Len = getSize(I, RMW->getValOperand());
        unsigned Align = getAlign(I, RMW->getPointerOperand());
        MA.emplace_back(I, RMW->getPointerOperand(), Len, Align, true);
        return 1;
    }

    if (CallInst *CI = getMemCmp(I)) {
        Value *Len = CI->getArgOperand(2);
        for (unsigned i = 0; i < 2; ++i) {
            Value *Ptr = CI->getArgOperand(i);
            MA.emplace_back(I, CI->getArgOperand(i), Len, getAlign(I, Ptr), true);
        }
        return 2;
    }

    return 0;
}

unsigned MemWrite::get(Instruction &I, SmallVectorImpl<MemAccess> &MA) {
    if (StoreInst *SI = dyn_cast<StoreInst>(&I)) {
        Value *Len = getSize(I, SI->getValueOperand());
        MA.emplace_back(I, SI->getPointerOperand(), Len, SI->getAlignment(), false);
        return 1;
    }

    if (MemIntrinsic *MI = dyn_cast<MemIntrinsic>(&I)) {
        MA.emplace_back(I, MI->getRawDest(), MI->getLength(), MI->getAlignment(), false);
        return 1;
    }

    if (AtomicCmpXchgInst *CX = dyn_cast<AtomicCmpXchgInst>(&I)) {
        Value *Len = getSize(I, CX->getCompareOperand());
        unsigned Align = getAlign(I, CX->getPointerOperand());
        MA.emplace_back(I, CX->getPointerOperand(), Len, Align, false);
        return 1;
    }

    if (AtomicRMWInst *RMW = dyn_cast<AtomicRMWInst>(&I)) {
        Value *Len = getSize(I, RMW->getValOperand());
        unsigned Align = getAlign(I, RMW->getPointerOperand());
        MA.emplace_back(I, RMW->getPointerOperand(), Len, Align, false);
        return 1;
    }

    return 0;
}
