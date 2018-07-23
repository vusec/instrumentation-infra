/*
 * CustomInliner.cpp
 *
 *  Created on: Nov 10, 2015
 *      Author: haller, taddeus
 */

#include <llvm/Transforms/IPO/Inliner.h>
#include <llvm/Analysis/InlineCost.h>

#include "Common.h"

using namespace llvm;

struct CustomInliner : public LegacyInlinerBase {
    static char ID;
    CustomInliner() : LegacyInlinerBase(ID) {}

    InlineCost getInlineCost(CallSite CS) {
        if (Function *F = CS.getCalledFunction()) {
            // TODO remove __attribute__((unused)) from inline noinstrument helpers

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
