#define DEBUG_TYPE "sizeof-types"

#include "Common.h"
#include "SizeofTypes.h"

using namespace llvm;

Type *SizeofTypes::getSizeofType(Instruction *CI) {
    return mallocTypes.lookup(CI);
}

void SizeofTypes::setSizeofType(Instruction *CI, Type *Ty) {
    mallocTypes[CI] = Ty;
}

void SizeofTypes::getAnalysisUsage(AnalysisUsage &AU) const {
    AU.setPreservesAll();
}

bool SizeofTypes::runOnModule(Module &M) {
    unsigned Count = 0;
    DenseMap<Instruction*, Type*> Propagate;

    for (Function &F : M) {
        for (Instruction &I : instructions(F)) {
            ifncast(CallInst, CI, &I)
                continue;

            MDNode *MD = CI->getMetadata("sizeofglob");
            if (!MD)
                continue;

            ConstantAsMetadata *MDC = cast<ConstantAsMetadata>(MD->getOperand(0));
            ConstantAggregateZero *C = cast<ConstantAggregateZero>(MDC->getValue());
            Type *Ty = cast<StructType>(C->getType())->getElementType(0);

            // check for pattern call; cast(call); memset(call, ..., sizeof (castty)) and
            // propagate the sizeof type from memset to destination call,
            // assuming it is an allocation wrapper call followed by an
            // initializer (this happens a lot in perlbench/gcc)
            ifcast(MemSetInst, MI, CI) {
                ifcast(CallInst, Dst, MI->getDest()) {
                    if (Dst->getCalledFunction()) {
                        for (User *U : Dst->users()) {
                            ifncast(BitCastInst, BC, U)
                                continue;

                            // Only do this for struct types because bitcasts
                            // may directly cast to the first struct member
                            Type *Ty = BC->getDestTy()->getPointerElementType();
                            if (!Ty->isStructTy())
                                continue;

                            // Cancel the propagation if different bitcasts do not agree
                            auto it = Propagate.find(Dst);
                            if (it == Propagate.end()) {
                                Propagate[Dst] = Ty;
                            } else {
                                Type *KnownTy = it->second;
                                if (KnownTy && KnownTy != Ty)
                                    Propagate[Dst] = nullptr;
                            }
                        }
                    }
                }
            }
            // Ignore other intrinsics like memmove for now since we don't use
            // them
            else if (!isa<IntrinsicInst>(CI)) {
                DEBUG(LOG_LINE("Found sizeof type " << *Ty << " in " << F.getName()));
                mallocTypes[CI] = Ty;
                Count++;
            }

            // Remove metadat annotation since we now have the info in a
            // datastructure
            CI->setMetadata("sizeofglob", nullptr);
        }
    }

    unsigned int Propagated = 0;

    for (auto &it : Propagate) {
        Type *Ty = it.second;
        if (!Ty)
            continue;
        Instruction *CI = it.first;
        CallSite CS(CI);
        Function *F = CI->getParent()->getParent();
        Function *Wrapper = CS.getCalledFunction();
        DEBUG(LOG_LINE("Propagated sizeof type " << *Ty << " in " <<
                    F->getName() << " to " << Wrapper->getName() << " call"));
        mallocTypes[CI] = Ty;
        Count++;
        Propagated++;
    }

    LOG_LINE("Found sizeof type at " << Count << " callsites of which " <<
             Propagated << " where propagated");

    return Count > 0;
}

char SizeofTypes::ID = 0;
static RegisterPass<SizeofTypes> X("sizeof-types",
        "Replace source transformations by sizeof-types with constant sizes and store the type info",
        false, true);
