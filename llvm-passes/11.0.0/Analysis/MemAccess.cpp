#include <llvm/IR/Module.h>
#include <llvm/IR/InstIterator.h>
#include <llvm/IR/DataLayout.h>
#include <llvm/Support/raw_ostream.h>
#include <llvm/Support/CommandLine.h>
#include "Analysis/MemAccess.h"

using namespace llvm;

static cl::opt<bool>
OptDetectMemCmp("memaccess-memcmp",
        cl::desc("Detect calls to memcmp when scanning for memory accesses"),
        cl::init(false));

static CallInst *getMemCmp(Instruction &I) {
    if (OptDetectMemCmp) {
        // FIXME: use TargetLibraryInfo
        if (CallInst *CI = dyn_cast<CallInst>(&I)) {
            Function *F = CI->getCalledFunction();
            if (F && F->hasName() && F->getName() == "memcmp")
                return CI;
        }
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
    } else if (AnyMemTransferInst *MT = dyn_cast<AnyMemTransferInst>(I)) {
        if (IsRead)
            MT->setSource(P);
        else
            MT->setDest(P);
    } else if (AnyMemIntrinsic *MI = dyn_cast<AnyMemIntrinsic>(I)) {
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
    } else if (AnyMemSetInst *MS = dyn_cast<AnyMemSetInst>(I)) {
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
    return V->getPointerAlignment(DL).value();
}

unsigned MemAccess::get(Instruction &I, SmallVectorImpl<MemAccess> &MA) {
    unsigned OldSize = MA.size();

    if (LoadInst *LI = dyn_cast<LoadInst>(&I)) {
        MA.emplace_back(I, LI->getPointerOperand(), getSize(I, LI), LI->getAlignment(), true);
    }
    else if (StoreInst *SI = dyn_cast<StoreInst>(&I)) {
        Value *Len = getSize(I, SI->getValueOperand());
        MA.emplace_back(I, SI->getPointerOperand(), Len, SI->getAlignment(), false);
    }
    else if (AtomicCmpXchgInst *CX = dyn_cast<AtomicCmpXchgInst>(&I)) {
        Value *Ptr = CX->getPointerOperand();
        Value *Len = getSize(I, CX->getCompareOperand());
        unsigned Align = getAlign(I, Ptr);
        MA.emplace_back(I, Ptr, Len, Align, true);
        MA.emplace_back(I, Ptr, Len, Align, false);
    }
    else if (AtomicRMWInst *RMW = dyn_cast<AtomicRMWInst>(&I)) {
        Value *Ptr = RMW->getPointerOperand();
        Value *Len = getSize(I, RMW->getValOperand());
        unsigned Align = getAlign(I, Ptr);
        MA.emplace_back(I, Ptr, Len, Align, true);
        MA.emplace_back(I, Ptr, Len, Align, false);
    }
    else if (AnyMemIntrinsic *MI = dyn_cast<AnyMemIntrinsic>(&I)) {
        if (AnyMemTransferInst *MT = dyn_cast<AnyMemTransferInst>(&I))
            MA.emplace_back(I, MT->getRawSource(), MT->getLength(), MT->getSourceAlignment(), true);
        MA.emplace_back(I, MI->getRawDest(), MI->getLength(), MI->getDestAlignment(), false);
    }
    else if (CallInst *CI = getMemCmp(I)) {
        Value *Len = CI->getArgOperand(2);
        for (unsigned i = 0; i < 2; ++i) {
            Value *Ptr = CI->getArgOperand(i);
            MA.emplace_back(I, Ptr, Len, getAlign(I, Ptr), true);
        }
    }

    return MA.size() - OldSize;
}

static inline const SCEV *getSCEV(ScalarEvolution &SE, Value *V) {
    if (!SE.isSCEVable(V->getType()))
        return nullptr;
    return SE.getSCEV(V);
}

const SCEV *MemAccess::getStartSCEV(ScalarEvolution &SE) const {
    return getSCEV(SE, Pointer->stripPointerCasts());
}

const SCEV *MemAccess::getLengthSCEV(ScalarEvolution &SE) const {
    return getSCEV(SE, Length);
}

const SCEV *MemAccess::getEndSCEV(ScalarEvolution &SE) const {
    if (const SCEV *S = getStartSCEV(SE)) {
        if (const SCEV *L = getLengthSCEV(SE))
            return SE.getAddExpr(S, L, SCEV::FlagNSW);
    }
    return nullptr;
}
