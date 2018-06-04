#ifndef REPORT_H
#define REPORT_H

#include <stdio.h>
#include <stdbool.h>

#define PREFIX "[setup-report] "
#define report(...) fprintf(stderr, PREFIX __VA_ARGS__);
#define REPORT(key, value, format) report("%s: " format "\n", (key), (value))

#ifdef __cplusplus
extern "C" {
#endif

static inline void reportb(const char *key, bool value) {
    REPORT(key, value ? "True" : "False", "%s");
}

static inline void reporti(const char *key, long value) {
    REPORT(key, value, "%ld");
}

static inline void reportf(const char *key, float value) {
    REPORT(key, value, "%.6f");
}

static inline void reports(const char *key, char *value) {
    REPORT(key, value, "%s");
}

static inline void report_begin() {
    report("begin\n");
}

static inline void report_end() {
    report("end\n");
    fflush(stderr);                                            \
}

#ifdef __cplusplus
}
#endif

#undef REPORT

#endif /* REPORT_H */
