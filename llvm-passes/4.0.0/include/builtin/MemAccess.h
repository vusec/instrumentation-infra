#ifndef MEMACCESS_H
#define MEMACCESS_H

#include <llvm/IR/Value.h>
#include <llvm/IR/Instructions.h>
#include <llvm/IR/IntrinsicInst.h>
#include <llvm/IR/Constants.h>
#include <llvm/ADT/SmallVector.h>

using namespace llvm;

class MemAccess {
protected:
    Instruction *I;
    Value *Pointer;
    Value *Length;
    unsigned Alignment;
    bool IsRead;

    MemAccess(Instruction &I, Value *P, Value *L, unsigned A, bool R)
        : I(&I), Pointer(P), Length(L), Alignment(A), IsRead(R) {}

public:
    MemAccess() : I(nullptr) {}
    MemAccess(const MemAccess&) = default;
    ~MemAccess() = default;

    Instruction *getInstruction() const { return I; }
    Value *getPointer() const           { return Pointer; }
    Value *getLength() const            { return Length; }
    unsigned getAlignment() const       { return Alignment; }
    bool isRead() const                 { return IsRead; }
    bool isWrite() const                { return !IsRead; }

    bool hasConstLength() const         { return isa<Constant>(Length); }
    uint64_t getConstLength() const     { return cast<ConstantInt>(Length)->getZExtValue(); }

    bool isValid() const                { return I != nullptr; }
    operator bool() const               { return isValid(); }

    //bool isInBounds() const; FIXME: use AllocSite

    template<unsigned size = 16>
    static inline SmallVector<MemAccess, size> collect(Function &F) {
        SmallVector<MemAccess, 16> L;
        collect(F, L);
        return L;
    }
    static void collect(Function &F, SmallVectorImpl<MemAccess> &L);
};

class MemRead : public MemAccess {
    MemRead() : MemAccess() {}
public:
    MemRead(const MemRead&) = default;
    ~MemRead() = default;

    MemRead(LoadInst &LI);
    MemRead(MemTransferInst &MT);
    MemRead(AtomicCmpXchgInst &CX);
    MemRead(AtomicRMWInst &RMW);

    static const MemRead TryCreate(Instruction &I);
};

class MemWrite : public MemAccess {
    MemWrite() : MemAccess() {}
public:
    MemWrite(const MemWrite&) = default;
    ~MemWrite() = default;

    MemWrite(StoreInst &SI);
    MemWrite(MemIntrinsic &MI);
    MemWrite(AtomicCmpXchgInst &CX);
    MemWrite(AtomicRMWInst &RMW);

    static const MemWrite TryCreate(Instruction &I);
};

#endif /* !MEMACCESS_H */
