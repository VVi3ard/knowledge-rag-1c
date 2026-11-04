using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Text;
using System.Threading;
using System.Threading.Tasks;

namespace KnowledgeRagBridge;

internal static class Program
{
    private const string BaseUrl = "http://127.0.0.1:8179";
    private const string HealthUrl = $"{BaseUrl}/health";
    private const string SseUrl = $"{BaseUrl}/sse";
    private const string BaseDir = @"Z:\knowledge-rag";
    private const string PythonwPath = @"Z:\knowledge-rag\venv\Scripts\pythonw.exe";
    private const string PidFilePath = @"Z:\knowledge-rag\data\daemon.pid";
    private const string LockFilePath = @"Z:\knowledge-rag\data\knowledge-rag.lock";
    private const string LogFilePath = @"Z:\knowledge-rag\bridge.log";

    public static async Task Main(string[] args)
    {
        try
        {
            await RunBridgeAsync();
        }
        catch (Exception ex)
        {
            Log($"Unhandled fatal exception: {ex}");
        }
        finally
        {
            OnClientExit();
        }
    }

    private static async Task RunBridgeAsync()
    {
        await EnsureDaemonRunningAsync();

        using var cts = new CancellationTokenSource();
        using var sseHttpClient = new HttpClient { Timeout = Timeout.InfiniteTimeSpan };
        using var postHttpClient = new HttpClient { Timeout = TimeSpan.FromSeconds(60) };

        using var stdinStream = Console.OpenStandardInput();
        using var stdinReader = new StreamReader(stdinStream, Encoding.UTF8);

        using var stdoutStream = Console.OpenStandardOutput();
        using var stdoutWriter = new StreamWriter(stdoutStream, new UTF8Encoding(false)) { AutoFlush = true };

        var endpointTcs = new TaskCompletionSource<string>();

        // SSE reader task
        var sseTask = Task.Run(async () =>
        {
            try
            {
                using var request = new HttpRequestMessage(HttpMethod.Get, SseUrl);
                request.Headers.Accept.ParseAdd("text/event-stream");

                using var response = await sseHttpClient.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, cts.Token);
                response.EnsureSuccessStatusCode();

                using var stream = await response.Content.ReadAsStreamAsync(cts.Token);
                using var reader = new StreamReader(stream, Encoding.UTF8);

                string? currentEvent = null;

                while (!cts.Token.IsCancellationRequested)
                {
                    var line = await reader.ReadLineAsync(cts.Token);
                    if (line == null) break;

                    if (line.StartsWith("event: "))
                    {
                        currentEvent = line.Substring(7).Trim();
                    }
                    else if (line.StartsWith("data: "))
                    {
                        var data = line.Substring(6);

                        if (currentEvent == "endpoint" && !endpointTcs.Task.IsCompleted)
                        {
                            var target = data.Trim();
                            var fullUrl = target.StartsWith("http", StringComparison.OrdinalIgnoreCase)
                                ? target
                                : $"{BaseUrl}{target}";
                            endpointTcs.TrySetResult(fullUrl);
                        }
                        else
                        {
                            await stdoutWriter.WriteLineAsync(data);
                        }
                    }
                    else if (string.IsNullOrWhiteSpace(line))
                    {
                        currentEvent = null;
                    }
                }
            }
            catch (OperationCanceledException) { }
            catch (Exception ex)
            {
                Log($"SSE stream error: {ex.Message}");
                endpointTcs.TrySetException(ex);
            }
        }, cts.Token);

        // Wait for endpoint URL from server
        var messageUrl = await endpointTcs.Task;

        // Stdin forwarding task
        var stdinTask = Task.Run(async () =>
        {
            try
            {
                while (!cts.Token.IsCancellationRequested)
                {
                    var line = await stdinReader.ReadLineAsync();
                    if (line == null)
                    {
                        // EOF - parent process (Gemini / Claude) disconnected
                        break;
                    }

                    if (string.IsNullOrWhiteSpace(line))
                        continue;

                    var content = new StringContent(line, Encoding.UTF8, "application/json");
                    var postResponse = await postHttpClient.PostAsync(messageUrl, content, cts.Token);
                    postResponse.EnsureSuccessStatusCode();
                }
            }
            catch (Exception ex)
            {
                Log($"Stdin forwarding error: {ex.Message}");
            }
            finally
            {
                cts.Cancel();
            }
        });

        await Task.WhenAny(stdinTask, sseTask);
        cts.Cancel();
    }

    private static async Task EnsureDaemonRunningAsync()
    {
        using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(2) };

        try
        {
            var res = await client.GetAsync(HealthUrl);
            if (res.IsSuccessStatusCode)
                return;
        }
        catch { }

        // Start daemon in background without ANY console window
        var psi = new ProcessStartInfo
        {
            FileName = PythonwPath,
            Arguments = "-m mcp_server.server",
            WorkingDirectory = BaseDir,
            CreateNoWindow = true,
            UseShellExecute = false,
            WindowStyle = ProcessWindowStyle.Hidden,
        };

        psi.EnvironmentVariables["KNOWLEDGE_RAG_DIR"] = BaseDir;
        psi.EnvironmentVariables["PYTHONPATH"] = Path.Combine(BaseDir, "venv", "Lib", "site-packages");
        psi.EnvironmentVariables["VIRTUAL_ENV"] = Path.Combine(BaseDir, "venv");
        psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";

        var proc = Process.Start(psi);
        if (proc != null)
        {
            try
            {
                Directory.CreateDirectory(Path.GetDirectoryName(PidFilePath)!);
                File.WriteAllText(PidFilePath, proc.Id.ToString());
            }
            catch { }
        }

        // Wait up to 20 seconds for /health
        for (int i = 0; i < 40; i++)
        {
            await Task.Delay(500);
            try
            {
                var check = await client.GetAsync(HealthUrl);
                if (check.IsSuccessStatusCode)
                {
                    Log($"Daemon started successfully on PID {(proc != null ? proc.Id : -1)}");
                    return;
                }
            }
            catch { }
        }

        throw new InvalidOperationException("Knowledge-RAG background daemon failed to respond to health check within 20s.");
    }

    private static void OnClientExit()
    {
        try
        {
            // Give 1 second grace period for other connections/reloads
            Thread.Sleep(1000);

            var myPid = Environment.ProcessId;
            var remainingBridges = Process.GetProcessesByName("knowledge-rag-bridge")
                                          .Where(p => p.Id != myPid)
                                          .ToList();

            if (remainingBridges.Count == 0)
            {
                Log("Last client exited. Shutting down Knowledge-RAG daemon...");
                KillDaemonProcess();
            }
            else
            {
                Log($"Client exited, but {remainingBridges.Count} other bridge(s) still active. Keeping daemon alive.");
            }
        }
        catch (Exception ex)
        {
            Log($"Error in OnClientExit: {ex.Message}");
        }
    }

    private static void KillDaemonProcess()
    {
        try
        {
            // 1. Try reading saved PID
            if (File.Exists(PidFilePath))
            {
                var pidText = File.ReadAllText(PidFilePath).Trim();
                if (int.TryParse(pidText, out int pid))
                {
                    try
                    {
                        var p = Process.GetProcessById(pid);
                        if (!p.HasExited)
                        {
                            p.Kill(true);
                            p.WaitForExit(3000);
                        }
                    }
                    catch { }
                }
                try { File.Delete(PidFilePath); } catch { }
            }

            // 2. Also find and kill any stray pythonw running for knowledge-rag
            var pythonwProcesses = Process.GetProcessesByName("pythonw");
            foreach (var p in pythonwProcesses)
            {
                try
                {
                    var path = p.MainModule?.FileName;
                    if (path != null && (path.Contains("knowledge-rag", StringComparison.OrdinalIgnoreCase) || path.Contains("uv", StringComparison.OrdinalIgnoreCase)))
                    {
                        p.Kill(true);
                        p.WaitForExit(2000);
                    }
                }
                catch { }
            }

            // 3. Clean up lock file if present
            if (File.Exists(LockFilePath))
            {
                try { File.Delete(LockFilePath); } catch { }
            }

            Log("Daemon stopped and resources released.");
        }
        catch (Exception ex)
        {
            Log($"KillDaemonProcess error: {ex.Message}");
        }
    }

    private static void Log(string message)
    {
        try
        {
            var line = $"[{DateTime.Now:yyyy-MM-dd HH:mm:ss}] {message}{Environment.NewLine}";
            File.AppendAllText(LogFilePath, line, Encoding.UTF8);
        }
        catch { }
    }
}
