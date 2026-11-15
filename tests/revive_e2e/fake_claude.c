/*
 * A stand-in for Claude Code in revive's end-to-end test (e2e.py, beside it), built there as `claude` so ps and athena's agents.py take it
 * for one. It writes the words it was started with to LOG_DIR/<name>.argv, one per line, tells Rex it is idle (OSC 7501), draws
 * an empty input box, and then appends each line typed into it to LOG_DIR/<name>.typed until its terminal closes. LOG_DIR is built in
 * (-DLOG_DIR), as a pane runs with the Rex server's environment, not the test's.
 */
#include <stdio.h>
#include <string.h>

static FILE *open_log(const char *name, const char *suffix, const char *mode) {
  char path[4096];
  snprintf(path, sizeof path, "%s/%s.%s", LOG_DIR, name, suffix);
  return fopen(path, mode);
}

int main(int argc, char **argv) {
  const char *name = "unnamed";
  for (int i = 1; i + 1 < argc; i++)
    if (!strcmp(argv[i], "--name")) name = argv[i + 1];
  FILE *out = open_log(name, "argv", "w");
  if (!out) return 1;
  for (int i = 1; i < argc; i++) fprintf(out, "%s\n", argv[i]);
  fclose(out);
  printf("\033]7501;state=idle\007fake claude %s\n\xe2\x9d\xaf ", name);
  fflush(stdout);
  char line[8192];
  while (fgets(line, sizeof line, stdin)) {
    FILE *typed = open_log(name, "typed", "a");
    if (!typed) return 1;
    fputs(line, typed);
    fclose(typed);
    printf("\n\xe2\x9d\xaf ");
    fflush(stdout);
  }
  return 0;
}
