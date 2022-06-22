#ifndef _DUMP_IR_HELPER_H
#define _DUMP_IR_HELPER_H

#include "noinstrument.h"

#ifdef __cplusplus
extern "C" {
#endif

// For DumpIR pass output
// XXX could also use __FILE__?
#define DEBUG_MODULE_NAME(n) \
    __attribute__((used)) \
    static const char NOINSTRUMENT(DEBUG_MODULE_NAME)[] = (n);

#ifdef __cplusplus
}
#endif

#endif /* !_DUMP_IR_HELPER_H */
