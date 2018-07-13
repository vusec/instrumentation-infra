#ifndef MEMACCESS_H
#define MEMACCESS_H

#include <llvm/IR/Value.h>
#include <llvm/IR/Instructions.h>
#include <llvm/IR/IntrinsicInst.h>
#include <llvm/IR/InstIterator.h>
#include <llvm/IR/Constants.h>
#include <llvm/Support/raw_ostream.h>
#include <llvm/Support/Debug.h>
#include <string>
#include <iterator>

#include <llvm/Support/raw_ostream.h>

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
    Value *getPointer()           const { return Pointer; }
    Value *getLength()            const { return Length; }
    bool hasReadValue()           const { return isa<LoadInst>(I); }
    Value *getReadValue()         const { return cast<LoadInst>(I); }
    unsigned getAlignment()       const { return Alignment; }
    bool isRead()                 const { return IsRead; }
    bool isWrite()                const { return !IsRead; }

    bool hasConstLength()         const { return isa<ConstantInt>(Length); }
    uint64_t getConstLength()     const { return cast<ConstantInt>(Length)->getZExtValue(); }

    bool isValid()                const { return I != nullptr; }
    operator bool()               const { return isValid(); }

    void dump()                   const { dbgs() << toString() << "\n"; }
    void print(raw_ostream &O)    const;
    const std::string toString()  const;
};

class MemRead : public MemAccess {
public:
    MemRead() : MemAccess() {}
    MemRead(const MemRead&) = default;
    ~MemRead() = default;

    MemRead(LoadInst &LI);
    MemRead(MemTransferInst &MT);
    MemRead(AtomicCmpXchgInst &CX);
    MemRead(AtomicRMWInst &RMW);
    MemRead(Instruction &I);
    static const MemRead Create(Instruction &I);
};

class MemWrite : public MemAccess {
public:
    MemWrite() : MemAccess() {}
    MemWrite(const MemWrite&) = default;
    ~MemWrite() = default;

    MemWrite(StoreInst &SI);
    MemWrite(MemIntrinsic &MI);
    MemWrite(AtomicCmpXchgInst &CX);
    MemWrite(AtomicRMWInst &RMW);
    MemWrite(Instruction &I);
    static const MemWrite Create(Instruction &I);
};

template<typename inst_iterator>
class MemAccessIterator {
    inst_iterator I, E;
    bool triedRead;
    MemAccess MA;

public:
    typedef std::input_iterator_tag iterator_category;
    typedef signed                  difference_type;
    typedef const MemAccess         value_type;
    typedef const MemAccess*        pointer;
    typedef const MemAccess&        reference;

    MemAccessIterator() = delete;
    MemAccessIterator(inst_iterator B, inst_iterator E)
        : I(B), E(E) { advanceToFirstValidAccess(); }
    MemAccessIterator(const MemAccessIterator &y)
        : I(y.I), E(y.E), triedRead(y.triedRead), MA(y.MA) {}
    ~MemAccessIterator() = default;

    inline bool operator==(const MemAccessIterator &y) const {
        return I == y.I;
    }
    inline bool operator!=(const MemAccessIterator &y) const {
        return !operator==(y);
    }

    MemAccessIterator& operator++() {
        do {
            if (triedRead) {
                MA = MemWrite::Create(*I);
                triedRead = false;
            } else if (++I != E) {
                MA = MemRead::Create(*I);
                triedRead = true;
            }
        } while (!MA.isValid() && !atEnd());
        return *this;
    }
    inline MemAccessIterator operator++(int) {
        MemAccessIterator tmp = *this;
        ++*this;
        return tmp;
    }

    inline reference operator*()  const { return MA; }
    inline pointer   operator->() const { return &operator*(); }

    inline bool atEnd() const { return I == E; }

private:
    void advanceToFirstValidAccess() {
        if (!atEnd()) {
            MA = MemRead::Create(*I);
            triedRead = true;
            if (!MA.isValid())
                operator++();
        }
    }
};

typedef MemAccessIterator<inst_iterator>        func_memaccess_iterator;
typedef iterator_range<func_memaccess_iterator> func_memaccess_range;
typedef MemAccessIterator<BasicBlock::iterator> bb_memaccess_iterator;
typedef iterator_range<bb_memaccess_iterator>   bb_memaccess_range;

static inline func_memaccess_iterator memaccess_begin(Function &F) {
    return func_memaccess_iterator(inst_begin(F), inst_end(F));
}
static inline func_memaccess_iterator memaccess_end(Function &F) {
    return func_memaccess_iterator(inst_end(F), inst_end(F));
}
static inline func_memaccess_range memaccesses(Function &F) {
    return func_memaccess_range(memaccess_begin(F), memaccess_end(F));
}

static inline bb_memaccess_iterator memaccess_begin(BasicBlock &BB) {
    return bb_memaccess_iterator(BB.begin(), BB.end());
}
static inline bb_memaccess_iterator memaccess_end(BasicBlock &BB) {
    return bb_memaccess_iterator(BB.end(), BB.end());
}
static inline bb_memaccess_range memaccesses(BasicBlock &BB) {
    return bb_memaccess_range(memaccess_begin(BB), memaccess_end(BB));
}

static inline bb_memaccess_iterator memaccess_begin(Instruction &I) {
    BasicBlock::iterator B(I), E = B;
    return bb_memaccess_iterator(B, ++E);
}
static inline bb_memaccess_iterator memaccess_end(Instruction &I) {
    BasicBlock::iterator E(I);
    return bb_memaccess_iterator(++E, E);
}
static inline bb_memaccess_range memaccesses(Instruction &I) {
    return bb_memaccess_range(memaccess_begin(I), memaccess_end(I));
}

#endif // MEMACCESS_H
