/*
 * CustomInliner.cpp
 *
 *  Created on: Nov 10, 2015
 *      Author: haller, taddeus
 */

#include <llvm/Transforms/IPO/InlinerPass.h>
#include <llvm/Analysis/InlineCost.h>

#include "utils/Common.h"

using namespace llvm;

struct CustomInliner : public Inliner {
    static char ID;
    CustomInliner() : Inliner(ID) {}

    InlineCost getInlineCost(CallSite CS) {
        if (Function *F = CS.getCalledFunction()) {
            if (isNoInstrument(F) && F->hasFnAttribute(Attribute::AlwaysInline))
                return InlineCost::getAlways();

            if (F->hasName() && F->getName().startswith(NOINSTRUMENT_PREFIX "_inline_"))
                return InlineCost::getAlways();
        }

        return InlineCost::getNever();
    }
};

char CustomInliner::ID = 0;
static RegisterPass<CustomInliner> X("custominline", "Custom Inliner Pass", true, false);
