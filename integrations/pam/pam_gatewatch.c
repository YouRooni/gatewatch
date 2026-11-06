#define _GNU_SOURCE
#include <security/pam_modules.h>
#include <security/pam_ext.h>
#include <arpa/inet.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

static pid_t parent_pid(pid_t pid) {
    char path[64], buffer[4096], state;
    long parent = 0;
    snprintf(path, sizeof(path), "/proc/%ld/stat", (long)pid);
    FILE *file = fopen(path, "re");
    if (!file) return 0;
    char *line = fgets(buffer, sizeof(buffer), file);
    fclose(file);
    char *end = line ? strrchr(buffer, ')') : NULL;
    if (!end || sscanf(end + 2, "%c %ld", &state, &parent) != 2) return 0;
    return (pid_t)parent;
}

PAM_EXTERN int pam_sm_authenticate(pam_handle_t *pamh, int flags, int argc, const char **argv) {
    (void)flags;
    const char *path = "/run/gatewatch/passwords.sock";
    for (int index = 0; index < argc; ++index)
        if (!strncmp(argv[index], "socket=", 7)) path = argv[index] + 7;
    const void *service = NULL, *host = NULL, *user = NULL;
    if (geteuid() != 0 || pam_get_item(pamh, PAM_SERVICE, &service) != PAM_SUCCESS || !service)
        return PAM_IGNORE;
    if (strcmp(service, "sshd") && strncmp(service, "gatewatch-test", 14)) return PAM_IGNORE;
    if (pam_get_item(pamh, PAM_RHOST, &host) != PAM_SUCCESS || !host || !*(const char *)host)
        return PAM_IGNORE;
    if (pam_get_item(pamh, PAM_USER, &user) != PAM_SUCCESS || !user) return PAM_IGNORE;
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    if (strlen(path) >= sizeof(address.sun_path)) return PAM_IGNORE;
    memcpy(address.sun_path, path, strlen(path) + 1);
    int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if (fd < 0) return PAM_IGNORE;
    if (connect(fd, (struct sockaddr *)&address, sizeof(address))) {
        close(fd);
        return PAM_IGNORE;
    }
    const char *password = NULL;
    if (pam_get_authtok(pamh, PAM_AUTHTOK, &password, NULL) != PAM_SUCCESS || !password) {
        close(fd);
        return PAM_IGNORE;
    }
    size_t user_len = strnlen(user, 257), host_len = strnlen(host, 65), secret_len = strnlen(password, 513);
    if (user_len > 256 || host_len > 64 || secret_len > 512) {
        close(fd);
        return PAM_IGNORE;
    }
    uint32_t header[12] = {htonl(0x47575031)};
    pid_t pid = getpid();
    for (int index = 0; index < 2 && pid > 1; ++index) {
        header[index + 1] = htonl((uint32_t)pid);
        pid = parent_pid(pid);
    }
    struct timespec captured;
    if (clock_gettime(CLOCK_REALTIME, &captured)) {
        close(fd);
        return PAM_IGNORE;
    }
    header[3] = htonl((uint32_t)captured.tv_sec);
    header[4] = htonl((uint32_t)captured.tv_nsec);
    header[9] = htonl((uint32_t)user_len);
    header[10] = htonl((uint32_t)host_len);
    header[11] = htonl((uint32_t)secret_len);
    unsigned char packet[48 + 256 + 64 + 512];
    size_t size = sizeof(header);
    memcpy(packet, header, size);
    memcpy(packet + size, user, user_len); size += user_len;
    memcpy(packet + size, host, host_len); size += host_len;
    memcpy(packet + size, password, secret_len); size += secret_len;
    (void)send(fd, packet, size, MSG_DONTWAIT | MSG_NOSIGNAL);
    explicit_bzero(packet, sizeof(packet));
    close(fd);
    return PAM_IGNORE;
}

PAM_EXTERN int pam_sm_setcred(pam_handle_t *pamh, int flags, int argc, const char **argv) {
    (void)pamh; (void)flags; (void)argc; (void)argv;
    return PAM_IGNORE;
}
