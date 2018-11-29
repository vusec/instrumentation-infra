#ifndef BUILTIN_MEM_ACCESS_H
#define BUILTIN_MEM_ACCESS_H

#include <llvm/IR/Value.h>
#include <llvm/IR/Instructions.h>
#include <llvm/IR/IntrinsicInst.h>
#include <llvm/IR/InstIterator.h>
#include <llvm/IR/Constants.h>
#include <llvm/ADT/SmallVector.h>
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

public:
    MemAccess(Instruction &I, Value *P, Value *L, unsigned A, bool R)
        : I(&I), Pointer(P), Length(L), Alignment(A), IsRead(R) {}
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

    static unsigned get(Instruction &I, SmallVectorImpl<MemAccess> &MA);
};

template<typename inst_iterator>
class MemAccessIterator {
    typedef SmallVector<MemAccess, 4> MAVec;
    inst_iterator I, E;
    MAVec MA;
    MAVec::iterator MAI;

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
        : I(B), E(E), MAI(MA.begin()) { advanceToFirstValidAccess(); }
    MemAccessIterator(const MemAccessIterator &y)
        : I(y.I), E(y.E), MA(y.MA), MAI(MA.begin() + y.offset()) {}
    ~MemAccessIterator() = default;

    inline bool operator==(const MemAccessIterator &y) const {
        return I == y.I && E == y.E && offset() == y.offset();
    }
    inline bool operator!=(const MemAccessIterator &y) const {
        return !operator==(y);
    }

    MemAccessIterator& operator++() {
        if (++MAI == MA.end()) {
            MA.clear();
            while (++I != E && MemAccess::get(*I, MA) == 0);
            MAI = MA.begin();
        }
        return *this;
    }
    inline MemAccessIterator operator++(int) {
        MemAccessIterator tmp = *this;
        ++*this;
        return tmp;
    }

    inline reference operator*()  const { return *MAI; }
    inline pointer   operator->() const { return &operator*(); }
    inline signed offset()        const { return MAI - MA.begin(); }
    inline bool atEnd()           const { return I == E && MAI == MA.end(); }

private:
    void advanceToFirstValidAccess() {
        while (I != E && MemAccess::get(*I, MA) == 0)
            ++I;
        MAI = MA.begin();
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

#endif // BUILTIN_MEM_ACCESS_H
