#ifndef _DUMP_IR_HELPER_H
#define _DUMP_IR_HELPER_H

/* For DumpIR pass output. */
/* XXX could also use __FILE__? */
#ifdef __cplusplus
#define DEBUG_MODULE_NAME(n) \
    extern "C" { \
        __attribute__((used)) \
        static const char NOINSTRUMENT(DEBUG_MODULE_NAME)[] = (n); \
    }
#else
#define DEBUG_MODULE_NAME(n) \
    __attribute__((used)) \
    static const char NOINSTRUMENT(DEBUG_MODULE_NAME)[] = (n);
#endif

#endif /* !_DUMP_IR_HELPER_H */
