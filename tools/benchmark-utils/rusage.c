#include <sys/time.h>
#include <sys/resource.h>
#include "report.h"

__attribute__((destructor))
static void report_rusage() {
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
    report_end();
}
