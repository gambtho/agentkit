package build

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

const (
	commandPathEnv = "PATH"
	commandLogEnv  = "COMMAND_LOG"
)

func TestRuntimeAdapterBuildTargetsHonorPlatform(t *testing.T) {
	tests := []struct {
		target     string
		dockerfile string
		image      string
	}{
		{target: "build-serve", dockerfile: "runtimes/pydantic-ai/Dockerfile", image: "agentkit-serve:platform-test"},
		{target: "build-serve-maf", dockerfile: "runtimes/microsoft-agent-framework/Dockerfile", image: "agentkit-serve-maf:platform-test"},
		{target: "build-serve-langgraph", dockerfile: "runtimes/langgraph/Dockerfile", image: "agentkit-serve-langgraph:platform-test"},
	}

	for _, tt := range tests {
		t.Run(tt.target, func(t *testing.T) {
			cmd := makeAdapterDryRunCommand(tt.target)
			cmd.Dir = filepath.Join("..", "..")
			out, err := cmd.CombinedOutput()
			if err != nil {
				t.Fatalf("make dry run failed: %v\n%s", err, out)
			}
			command := string(out)
			for _, want := range []string{
				"docker buildx build",
				"-f " + tt.dockerfile,
				"-t " + tt.image,
				"--platform linux/arm64",
				"--load",
			} {
				if !strings.Contains(command, want) {
					t.Fatalf("%s command = %q, want substring %q", tt.target, command, want)
				}
			}
		})
	}
}

func makeAdapterDryRunCommand(target string) *exec.Cmd {
	switch target {
	case "build-serve":
		return exec.Command("make", "--no-print-directory", "-n", "build-serve", "PLATFORM=linux/arm64", "TAG=platform-test")
	case "build-serve-maf":
		return exec.Command("make", "--no-print-directory", "-n", "build-serve-maf", "PLATFORM=linux/arm64", "TAG=platform-test")
	case "build-serve-langgraph":
		return exec.Command("make", "--no-print-directory", "-n", "build-serve-langgraph", "PLATFORM=linux/arm64", "TAG=platform-test")
	default:
		panic("unsupported adapter build target: " + target)
	}
}

func TestRunTestAgentIsHostReachableAndCapturesCurlToken(t *testing.T) {
	repoRoot, err := filepath.Abs(filepath.Join("..", ".."))
	if err != nil {
		t.Fatalf("resolve repository root: %v", err)
	}
	tempDir := t.TempDir()
	binDir := filepath.Join(tempDir, "bin")
	if err := os.Mkdir(binDir, 0o755); err != nil {
		t.Fatalf("create fake bin directory: %v", err)
	}
	commandLog := filepath.Join(tempDir, "commands.log")

	writeCommandStub(t, binDir, "docker", `
{
  printf 'docker'
  for arg in "$@"; do printf '\t%s' "$arg"; done
  printf '\n'
} >>"${COMMAND_LOG}"
`)

	const modelKey = "model-key-must-not-appear"
	const localToken = "command-capture-token"
	cmd := exec.Command(
		"make",
		"--no-print-directory",
		"run-test-agent",
		"PLATFORM=linux/arm64",
		"TAG=command-capture",
		"LOCAL_AUTH_TOKEN="+localToken,
	)
	cmd.Dir = repoRoot
	cmd.Env = replaceEnvironment(os.Environ(), map[string]string{
		commandLogEnv:    commandLog,
		"MAKEFLAGS":      "",
		"OPENAI_API_KEY": modelKey,
		commandPathEnv:   binDir + string(os.PathListSeparator) + os.Getenv(commandPathEnv),
	})
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("run-test-agent command capture failed: %v\n%s", err, out)
	}
	logBytes, err := os.ReadFile(commandLog)
	if err != nil {
		t.Fatalf("read command log: %v", err)
	}
	command := string(logBytes)
	for _, want := range []string{
		"\t-p\t127.0.0.1:8080:8080\t",
		"\t-e\tAGENTKIT_BIND=0.0.0.0\t",
		"\t-e\tAGENTKIT_AUTH_TOKEN=" + localToken + "\t",
		"\t-e\tOPENAI_API_KEY\t",
		"\thello-agent:command-capture\n",
	} {
		if !strings.Contains(command, want) {
			t.Fatalf("captured docker command = %q, want substring %q", command, want)
		}
	}
	combined := string(out) + command
	if strings.Contains(combined, modelKey) {
		t.Fatalf("run-test-agent output leaked model key: %q", combined)
	}
	for _, want := range []string{
		"Authorization: Bearer " + localToken,
		"http://127.0.0.1:8080/v1/models",
	} {
		if !strings.Contains(string(out), want) {
			t.Fatalf("run-test-agent output = %q, want substring %q", out, want)
		}
	}
}

func TestLiveAIKitScriptForwardsDetectedPlatformToAdapterBuild(t *testing.T) {
	repoRoot, err := filepath.Abs(filepath.Join("..", ".."))
	if err != nil {
		t.Fatalf("resolve repository root: %v", err)
	}
	tempDir := t.TempDir()
	binDir := filepath.Join(tempDir, "bin")
	if err := os.Mkdir(binDir, 0o755); err != nil {
		t.Fatalf("create fake bin directory: %v", err)
	}
	commandLog := filepath.Join(tempDir, "commands.log")

	writeCommandStub(t, binDir, "docker", `
{
  printf 'docker'
  for arg in "$@"; do printf '\t%s' "$arg"; done
  printf '\n'
} >>"${COMMAND_LOG}"
case "${1:-}" in
  info)
    case "$*" in
      *NCPU*) printf '8\n' ;;
      *) printf 'arm64\n' ;;
    esac
    ;;
  inspect) printf 'true\n' ;;
esac
`)
	writeCommandStub(t, binDir, "make", `
{
  printf 'make'
  for arg in "$@"; do printf '\t%s' "$arg"; done
  printf '\n'
} >>"${COMMAND_LOG}"
`)
	writeCommandStub(t, binDir, "curl", `
case "$*" in
  */v1/models*)
    printf '{"data":[{"id":"qwen-3.5-2b"}]}'
    ;;
  */v1/chat/completions*)
    printf '{"model":"qwen-3.5-2b","created":123,"choices":[{"message":{"content":"DONE42"}}]}'
    ;;
esac
`)
	writeCommandStub(t, binDir, "jq", `
case "$*" in
  *'.data[].id'*) printf 'qwen-3.5-2b\n' ;;
  *'{model,'*) printf '{"model":"qwen-3.5-2b","content":"DONE42"}\n' ;;
esac
`)
	writeCommandStub(t, binDir, "go", "")

	cmd := exec.Command("bash", "scripts/live-aikit-agent-e2e.sh")
	cmd.Dir = repoRoot
	cmd.Env = replaceEnvironment(os.Environ(), map[string]string{
		"AIKIT_IMAGE":  "",
		commandLogEnv:  commandLog,
		commandPathEnv: binDir + string(os.PathListSeparator) + os.Getenv(commandPathEnv),
		"PLATFORM":     "",
		"RUNNER_TEMP":  tempDir,
		"TAG":          "command-capture",
	})
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("live script command capture failed: %v\n%s", err, out)
	}
	logBytes, err := os.ReadFile(commandLog)
	if err != nil {
		t.Fatalf("read command log: %v", err)
	}
	want := "make\tbuild-serve-maf\tTAG=command-capture\tPLATFORM=linux/arm64\n"
	if !strings.Contains(string(logBytes), want) {
		t.Fatalf("captured commands = %q, want %q", logBytes, want)
	}
	commands := string(logBytes)
	for _, want := range []string{
		"ghcr.io/kaito-project/aikit/qwen3.5:2b@sha256:",
		"--cpus\t4", "--platform\tlinux/arm64", "--network-alias\taikit",
		"LOCALAI_FORCE_META_BACKEND_CAPABILITY=cpu", "--config-file=/config.yaml", "test/aikit-e2e/model.yaml,dst=/config.yaml,readonly",
	} {
		if !strings.Contains(commands, want) {
			t.Errorf("captured commands omit %q: %s", want, commands)
		}
	}
	for _, forbidden := range []string{"vekil", "COPILOT_GITHUB_TOKEN", "TOKEN_DIR"} {
		if strings.Contains(commands, forbidden) {
			t.Errorf("captured commands retain external provider dependency %q", forbidden)
		}
	}
}

func TestAIKitCPUQuotaUsesAvailableCPUs(t *testing.T) {
	repoRoot, err := filepath.Abs(filepath.Join("..", ".."))
	if err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct{ available, limit string }{
		{"1", "1"}, {"2", "2"}, {"4", "4"}, {"8", "4"}, {"0", ""}, {"invalid", ""},
	} {
		t.Run(tc.available, func(t *testing.T) {
			tempDir := t.TempDir()
			binDir := filepath.Join(tempDir, "bin")
			if err := os.Mkdir(binDir, 0o755); err != nil {
				t.Fatal(err)
			}
			logPath := filepath.Join(tempDir, "docker.log")
			writeCommandStub(t, binDir, "docker", `
if [ "$1" = info ]; then printf '%s\n' "$DAEMON_CPUS"; exit 0; fi
printf '%s\n' "$@" >"$COMMAND_LOG"
`)
			//nolint:gosec // Fixed shell source and repository/test-owned paths, not external input.
			cmd := exec.Command("bash", "-c", `source "$1"; start_aikit "$2" -d --name quota-test`,
				"quota", filepath.Join(repoRoot, "scripts", "aikit-e2e-common.sh"), filepath.Join(repoRoot, "test", "aikit-e2e", "model.yaml"))
			cmd.Env = replaceEnvironment(os.Environ(), map[string]string{
				commandPathEnv: binDir + string(os.PathListSeparator) + os.Getenv(commandPathEnv),
				"DAEMON_CPUS":  tc.available, commandLogEnv: logPath,
			})
			out, err := cmd.CombinedOutput()
			if tc.limit == "" {
				if err == nil {
					t.Fatalf("invalid CPU count accepted: %s", out)
				}
				if _, err := os.Stat(logPath); !os.IsNotExist(err) {
					t.Fatal("invalid CPU count started a container")
				}
				return
			}
			if err != nil {
				t.Fatalf("container startup failed: %v: %s", err, out)
			}
			commands, err := os.ReadFile(logPath)
			if err != nil {
				t.Fatal(err)
			}
			if !strings.Contains(string(commands), "--cpus\n"+tc.limit+"\n") {
				t.Fatalf("CPU quota = %s, want %s", commands, tc.limit)
			}
		})
	}
}

func TestAIKitWarmupRejectsInferenceFailures(t *testing.T) {
	if _, err := exec.LookPath("jq"); err != nil {
		t.Skip("jq is required for the live E2E warmup validator")
	}
	repoRoot, err := filepath.Abs(filepath.Join("..", ".."))
	if err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		name     string
		response string
		curlExit string
		wantOK   bool
	}{
		{"valid", `{"model":"qwen-3.5-2b","created":123,"choices":[{"message":{"content":"OK"}}]}`, "0", true},
		{"http-error", `{"error":"model unavailable"}`, "22", false},
		{"http-error-with-valid-json", `{"model":"qwen-3.5-2b","created":123,"choices":[{"message":{"content":"OK"}}]}`, "22", false},
		{"wrong-model", `{"model":"another-model","created":123,"choices":[{"message":{"content":"OK"}}]}`, "0", false},
		{"missing-created", `{"model":"qwen-3.5-2b","choices":[{"message":{"content":"OK"}}]}`, "0", false},
		{"empty-content", `{"model":"qwen-3.5-2b","created":123,"choices":[{"message":{"content":""}}]}`, "0", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			tempDir := t.TempDir()
			binDir := filepath.Join(tempDir, "bin")
			if err := os.Mkdir(binDir, 0o755); err != nil {
				t.Fatal(err)
			}
			writeCommandStub(t, binDir, "curl", `printf '%s' "$WARMUP_JSON"; exit "$CURL_EXIT"`)
			// The conditional disables Bash errexit inside the function, so a failed
			// curl must be propagated explicitly rather than hidden by valid JSON.
			//nolint:gosec // Fixed shell source and repository/test-owned paths, not external input.
			cmd := exec.Command("bash", "-c", `source "$1"; if warm_aikit "$2" "$3"; then exit 0; else exit 1; fi`,
				"warmup", filepath.Join(repoRoot, "scripts", "aikit-e2e-common.sh"), "http://aikit:8080", filepath.Join(tempDir, "response.json"))
			cmd.Env = replaceEnvironment(os.Environ(), map[string]string{
				commandPathEnv: binDir + string(os.PathListSeparator) + os.Getenv(commandPathEnv),
				"WARMUP_JSON":  tc.response, "CURL_EXIT": tc.curlExit,
			})
			out, err := cmd.CombinedOutput()
			if (err == nil) != tc.wantOK {
				t.Fatalf("warmup success = %v, want %v: %s", err == nil, tc.wantOK, out)
			}
		})
	}
}

func writeCommandStub(t *testing.T, dir, name, body string) {
	t.Helper()
	path := filepath.Join(dir, name)
	contents := "#!/bin/sh\nset -eu\n" + body
	if err := os.WriteFile(path, []byte(contents), 0o600); err != nil {
		t.Fatalf("write %s stub: %v", name, err)
	}
	if err := os.Chmod(path, 0o700); err != nil {
		t.Fatalf("make %s stub executable: %v", name, err)
	}
}

func replaceEnvironment(base []string, replacements map[string]string) []string {
	out := make([]string, 0, len(base)+len(replacements))
	for _, entry := range base {
		key, _, ok := strings.Cut(entry, "=")
		if ok {
			if _, replace := replacements[key]; replace {
				continue
			}
		}
		out = append(out, entry)
	}
	for key, value := range replacements {
		out = append(out, key+"="+value)
	}
	return out
}
