#include <sys/time.h>
#include <sys/resource.h>
#include "report.h"

static inline long long timediff_usec(struct timeval *t0, struct timeval *t1) {
    return (t1->tv_sec - t0->tv_sec) * 1000000LL + (t1->tv_usec - t0->tv_usec);
}

static inline double timediff_sec(struct timeval *t0, struct timeval *t1) {
    return timediff_usec(t0, t1) / (double)1000000LL;
}

static struct timeval starttime;

__attribute__((constructor))
static void start_timer() {
    gettimeofday(&starttime, NULL);
}

__attribute__((destructor))
static void report_stats() {
    struct timeval endtime;
    gettimeofday(&endtime, NULL);

    struct rusage u;

    if (getrusage(RUSAGE_SELF, &u) < 0) {
        perror("getrusage");
        return;
    }

    report_begin();
    reporti("_max_rss_kb", u.ru_maxrss);
    reporti("_sum_page_faults", u.ru_minflt + u.ru_majflt);
    reporti("_sum_io_operations", u.ru_inblock + u.ru_oublock);
    reporti("_sum_context_switches", u.ru_nvcsw + u.ru_nivcsw);
    reportfp("_sum_estimated_runtime_sec", timediff_sec(&starttime, &endtime), 3);
    report_end();
}
