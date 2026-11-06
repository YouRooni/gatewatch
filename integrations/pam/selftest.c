#define _GNU_SOURCE
#include <security/pam_appl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int conversation(int count, const struct pam_message **messages, struct pam_response **responses, void *data) {
    (void)data;
    struct pam_response *result = calloc((size_t)count, sizeof(*result));
    if (!result) return PAM_BUF_ERR;
    for (int index = 0; index < count; ++index) {
        if (messages[index]->msg_style == PAM_PROMPT_ECHO_OFF)
            result[index].resp = strdup(getenv("GATEWATCH_TEST_PASSWORD"));
        else if (messages[index]->msg_style == PAM_PROMPT_ECHO_ON)
            result[index].resp = strdup(getenv("GATEWATCH_TEST_USER"));
    }
    *responses = result;
    return PAM_SUCCESS;
}

int main(void) {
    if (!getenv("GATEWATCH_TEST_USER") || !getenv("GATEWATCH_TEST_PASSWORD")) return 2;
    pam_handle_t *handle = NULL;
    struct pam_conv conv = {conversation, NULL};
    int result = pam_start("gatewatch-test", getenv("GATEWATCH_TEST_USER"), &conv, &handle);
    if (result == PAM_SUCCESS) {
        pam_set_item(handle, PAM_RHOST, "203.0.113.42");
        result = pam_authenticate(handle, 0);
        pam_end(handle, result);
    }
    printf("%d\n", result);
    return 0;
}
