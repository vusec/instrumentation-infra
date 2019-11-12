#ifndef REPORT_H
#define REPORT_H

#include <stdio.h>
#include <stdbool.h>
#include <float.h>

#define PREFIX "[setup-report] "
#define report(...) fprintf(stderr, PREFIX __VA_ARGS__)
#define REPORT(format, key, ...) report("%s: " format "\n", (key), __VA_ARGS__)

#ifdef __cplusplus
extern "C" {
#endif

static inline void reportb(const char *key, bool value) {
    REPORT("%s", key, value ? "True" : "False");
}

static inline void reporti(const char *key, long long value) {
    REPORT("%lld", key, value);
}

static inline void reportfp(const char *key, double value, int precision) {
    REPORT("%.*f", key, precision, value);
}

static inline void reportf(const char *key, float value) {
    reportfp(key, (double)value, FLT_DECIMAL_DIG);
}

static inline void reportd(const char *key, double value) {
    reportfp(key, value, DBL_DECIMAL_DIG);
}

static inline void reports(const char *key, char *value) {
    REPORT("%s", key, value);
}

static inline void report_begin(const char *name) {
    report("begin %s\n", name);
}

static inline void report_end(const char *name) {
    report("end %s\n", name);
    fflush(stderr);                                            \
}

#ifdef __cplusplus
}
#endif

#undef REPORT

#endif /* REPORT_H */
