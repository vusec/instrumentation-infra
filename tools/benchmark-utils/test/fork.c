#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <sys/types.h>
#include <sys/wait.h>

int main() {
    const long psize = sysconf(_SC_PAGESIZE);
    volatile char *buf = (char*)malloc(100 * psize);

    // generate some page faults
    for (int i = 0; i < 50; i++)
        buf[i * psize] = 'a';

    puts("some I/O op");

    pid_t pid = fork();
    if (pid < 0) {
        perror("fork");
        return EXIT_FAILURE;
    }

    if (pid) {
        if (waitpid(pid, NULL, 0) < 0) {
            perror("waitpid");
            return EXIT_FAILURE;
        }
    } else {
        // generate one page fault
        buf[70 * psize] = 'a';
    }

    free((void*)buf);
    return EXIT_SUCCESS;
}
