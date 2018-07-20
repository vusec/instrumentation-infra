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

    inline Instruction *getInstruction() const { return I; }
    inline Value *getPointer()           const { return Pointer; }
    inline Value *getLength()            const { return Length; }
    inline bool hasValue()               const { return IsRead ? isa<LoadInst>(I) : isa<StoreInst>(I); }
    inline Value *getValue()             const { return IsRead ? cast<LoadInst>(I) :
                                                        cast<StoreInst>(I)->getValueOperand(); }
    inline unsigned getAlignment()       const { return Alignment; }
    inline bool isRead()                 const { return IsRead; }
    inline bool isWrite()                const { return !IsRead; }
    inline bool isAtomic()               const { return isa<AtomicCmpXchgInst>(I) || isa<AtomicRMWInst>(I); }
    inline bool hasConstLength()         const { return isa<ConstantInt>(Length); }
    inline uint64_t getConstLength()     const { return cast<ConstantInt>(Length)->getZExtValue(); }

    void setPointer(Value *P);
    void setValue(Value *V);

    inline bool isValid()  const { return I != nullptr; }
    inline operator bool() const { return isValid(); }

    inline void dump()           const { print(dbgs()); dbgs() << '\n'; }
    const std::string toString() const { std::string s; raw_string_ostream ss(s); print(ss); return s; }
    void print(raw_ostream &O)   const;
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
    static MemRead Create(Instruction &I);
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
    static MemWrite Create(Instruction &I);
};

template<typename inst_iterator>
class MemAccessIterator {
    inst_iterator I, E;
    bool triedRead;
    MemAccess MA;

public:
    typedef std::input_iterator_tag iterator_category;
    typedef signed                  difference_type;
    typedef MemAccess               value_type;
    typedef MemAccess*              pointer;
    typedef MemAccess&              reference;
    typedef const MemAccess*        const_pointer;
    typedef const MemAccess&        const_reference;

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

    inline reference       operator*()        { return MA; }
    inline const_reference operator*()  const { return MA; }
    inline pointer         operator->()       { return &operator*(); }
    inline const_pointer   operator->() const { return &operator*(); }

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

typedef MemAccessIterator<inst_iterator>        func_ma_iterator;
typedef iterator_range<func_ma_iterator>        func_ma_range;
typedef MemAccessIterator<BasicBlock::iterator> bb_ma_iterator;
typedef iterator_range<bb_ma_iterator>          bb_ma_range;

static inline func_ma_iterator ma_begin(Function &F) {
    return func_ma_iterator(inst_begin(F), inst_end(F));
}
static inline func_ma_iterator ma_end(Function &F) {
    return func_ma_iterator(inst_end(F), inst_end(F));
}
static inline func_ma_range memaccesses(Function &F) {
    return func_ma_range(ma_begin(F), ma_end(F));
}

static inline bb_ma_iterator ma_begin(BasicBlock &BB) {
    return bb_ma_iterator(BB.begin(), BB.end());
}
static inline bb_ma_iterator ma_end(BasicBlock &BB) {
    return bb_ma_iterator(BB.end(), BB.end());
}
static inline bb_ma_range memaccesses(BasicBlock &BB) {
    return bb_ma_range(ma_begin(BB), ma_end(BB));
}

static inline bb_ma_iterator ma_begin(Instruction &I) {
    BasicBlock::iterator B(I), E = B;
    return bb_ma_iterator(B, ++E);
}
static inline bb_ma_iterator ma_end(Instruction &I) {
    BasicBlock::iterator E(I);
    return bb_ma_iterator(++E, E);
}
static inline bb_ma_range memaccesses(Instruction &I) {
    return bb_ma_range(ma_begin(I), ma_end(I));
}

#endif // ma_H
