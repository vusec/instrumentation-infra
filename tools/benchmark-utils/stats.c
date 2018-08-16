#include <sys/time.h>
#include <sys/resource.h>
#include <sys/types.h>
#include <unistd.h>
#include <stdlib.h>
#include "report.h"

static inline long long timediff_usec(struct timeval *t0, struct timeval *t1) {
    return (t1->tv_sec - t0->tv_sec) * 1000000LL + (t1->tv_usec - t0->tv_usec);
}

static inline double timediff_sec(struct timeval *t0, struct timeval *t1) {
    return timediff_usec(t0, t1) / (double)1000000LL;
}

static inline long max(long a, long b) {
    return a > b ? a : b;
}

static pid_t startpid;
static struct timeval starttime;

__attribute__((constructor))
static void start_timer() {
    startpid = getpid();
    gettimeofday(&starttime, NULL);
}

__attribute__((destructor))
static void report_stats() {
    struct timeval endtime;
    gettimeofday(&endtime, NULL);

    // only report in parent process (assuming it survives the lifetimes of all
    // of its children)
    if (startpid != getpid())
        return;

    struct rusage u, child;

    // get resources used by parent
    if (getrusage(RUSAGE_SELF, &u) < 0) {
        perror("getrusage");
        exit(1);
    }

    // add resources used by children
    if (getrusage(RUSAGE_CHILDREN, &child) < 0) {
        perror("getrusage");
        exit(1);
    }

    u.ru_utime.tv_sec += child.ru_utime.tv_sec;
    u.ru_utime.tv_usec += child.ru_utime.tv_usec;
    u.ru_stime.tv_sec += child.ru_stime.tv_sec;
    u.ru_stime.tv_usec += child.ru_stime.tv_usec;
    u.ru_maxrss = max(u.ru_maxrss, child.ru_maxrss);
    u.ru_ixrss += child.ru_ixrss;
    u.ru_idrss += child.ru_idrss;
    u.ru_isrss += child.ru_isrss;
    u.ru_minflt += child.ru_minflt;
    u.ru_majflt += child.ru_majflt;
    u.ru_nswap += child.ru_nswap;
    u.ru_inblock += child.ru_inblock;
    u.ru_oublock += child.ru_oublock;
    u.ru_msgsnd += child.ru_msgsnd;
    u.ru_msgrcv += child.ru_msgrcv;
    u.ru_nsignals += child.ru_nsignals;
    u.ru_nvcsw += child.ru_nvcsw;
    u.ru_nivcsw += child.ru_nivcsw;

    // report accumulated results
    report_begin();
    reporti("_max_rss_kb", u.ru_maxrss);
    reporti("_sum_page_faults", u.ru_minflt + u.ru_majflt);
    reporti("_sum_io_operations", u.ru_inblock + u.ru_oublock);
    reporti("_sum_context_switches", u.ru_nvcsw + u.ru_nivcsw);
    reportfp("_sum_estimated_runtime_sec", timediff_sec(&starttime, &endtime), 3);
    report_end();
}
