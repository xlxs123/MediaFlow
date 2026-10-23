// MediaFlow WebView2 child-window host. .NET Framework / C# 5, no TCP endpoint.
using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;

namespace MediaFlow.WebView2Host
{
    internal sealed class HostError : Exception
    {
        internal readonly string Code;
        internal HostError(string code) { Code = code; }
    }

    internal sealed class PumpWindow : NativeWindow, IDisposable
    {
        private const int DispatchMessage = 0x8001;
        private readonly ConcurrentQueue<Action> actions = new ConcurrentQueue<Action>();
        [DllImport("user32.dll")]
        private static extern bool PostMessage(IntPtr hwnd, uint message, IntPtr wParam, IntPtr lParam);

        internal PumpWindow()
        {
            CreateHandle(new CreateParams { Caption = "", Parent = new IntPtr(-3), Style = 0 });
        }
        internal void Post(Action action)
        {
            actions.Enqueue(action);
            PostMessage(Handle, DispatchMessage, IntPtr.Zero, IntPtr.Zero);
        }
        protected override void WndProc(ref Message message)
        {
            if (message.Msg == DispatchMessage)
            {
                Action action;
                while (actions.TryDequeue(out action)) action();
            }
            base.WndProc(ref message);
        }
        public void Dispose() { DestroyHandle(); }
    }

    internal sealed class PumpContext : SynchronizationContext
    {
        private readonly PumpWindow pump;
        internal PumpContext(PumpWindow pump) { this.pump = pump; }
        public override void Post(SendOrPostCallback callback, object state)
        { pump.Post(delegate { callback(state); }); }
        public override SynchronizationContext CreateCopy() { return new PumpContext(pump); }
    }

    internal sealed class ChildWindow : NativeWindow, IDisposable
    {
        [DllImport("user32.dll")]
        private static extern bool SetWindowPos(IntPtr hwnd, IntPtr after, int x, int y, int width, int height, uint flags);
        [DllImport("user32.dll")]
        private static extern bool ShowWindow(IntPtr hwnd, int command);
        [DllImport("user32.dll", SetLastError = true)]
        private static extern IntPtr SetParent(IntPtr child, IntPtr parent);
        [DllImport("user32.dll", EntryPoint = "SetWindowLongPtrW", SetLastError = true)]
        private static extern IntPtr SetWindowLongPtr(IntPtr hwnd, int index, IntPtr value);
        [DllImport("user32.dll")]
        private static extern bool IsChild(IntPtr parent, IntPtr child);
        internal ChildWindow(int width, int height)
        {
            // Initialize on this STA's hidden own HWND, then switch the wrapper
            // to WS_CHILD and attach it to Qt before making anything visible.
            CreateHandle(new CreateParams { Caption = "", Parent = IntPtr.Zero,
                Style = unchecked((int)0x86000000),
                X = 0, Y = 0, Width = width, Height = height });
        }
        internal void Attach(IntPtr parent)
        {
            SetWindowLongPtr(Handle, -16, new IntPtr(unchecked((int)0x46000000)));
            SetParent(Handle, parent);
            if (!IsChild(parent, Handle)) throw new HostError("PARENT_ATTACH_FAILED");
        }
        internal void Resize(int width, int height)
        { if (Handle != IntPtr.Zero) SetWindowPos(Handle, IntPtr.Zero, 0, 0, width, height, 0x0014); }
        internal void Visible(bool visible)
        { if (Handle != IntPtr.Zero) ShowWindow(Handle, visible ? 8 : 0); }
        public void Dispose() { if (Handle != IntPtr.Zero) DestroyHandle(); }
    }

    internal sealed class PageState
    {
        internal string Id;
        internal CoreWebView2Controller Controller;
        internal ChildWindow Wrapper;
        internal CoreWebView2 Core;
        internal bool Closed;
        internal int Width, Height;
        internal bool Visible;
        internal long Generation;
        internal readonly Dictionary<int, int> Worlds = new Dictionary<int, int>();
        internal readonly List<CoreWebView2DevToolsProtocolEventReceiver> Receivers =
            new List<CoreWebView2DevToolsProtocolEventReceiver>();
        internal readonly TaskCompletionSource<PageState> Ready = new TaskCompletionSource<PageState>();
    }

    internal sealed class Host
    {
        private readonly PumpWindow pump;
        private readonly ApplicationContext application;
        private readonly Dictionary<string, PageState> pages = new Dictionary<string, PageState>();
        private readonly object outputLock = new object();
        // A bounded 128 MiB captured body may be escaped by CDP JSON and IPC
        // JSON. This is a serialization ceiling, never a preallocated buffer.
        private const int MaxResponseJsonCharacters = 128 * 1024 * 1024 * 8 + 1024 * 1024;
        private readonly JavaScriptSerializer json = new JavaScriptSerializer {
            MaxJsonLength = MaxResponseJsonCharacters, RecursionLimit = 128 };
        private CoreWebView2Environment environment;
        private CoreWebView2Controller keeper;
        private Task<object> initialization;
        private Task shutdown;
        private bool closing;
        private bool persistent;
        private bool clearingProfile;
        private readonly TaskCompletionSource<bool> browserExited = new TaskCompletionSource<bool>();
        private static readonly DateTime Epoch = new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc);
        private static readonly string[] CdpEvents = {
            "Network.requestWillBeSent", "Network.requestWillBeSentExtraInfo",
            "Network.responseReceived", "Network.responseReceivedExtraInfo",
            "Network.loadingFinished", "Network.loadingFailed", "Network.requestServedFromCache",
            "Network.dataReceived", "Runtime.executionContextCreated", "Runtime.executionContextDestroyed",
            "Runtime.executionContextsCleared", "Target.attachedToTarget", "Target.detachedFromTarget",
            "Target.targetCreated", "Target.targetDestroyed", "Target.targetInfoChanged",
            "Target.receivedMessageFromTarget", "Page.frameAttached", "Page.frameDetached",
            "Page.frameNavigated", "Page.frameStartedLoading", "Page.frameStoppedLoading",
            "Page.navigatedWithinDocument", "Page.domContentEventFired", "Page.loadEventFired", "Page.lifecycleEvent"
        };
        [DllImport("user32.dll")]
        private static extern bool IsWindow(IntPtr hwnd);
        [DllImport("user32.dll")]
        private static extern short GetKeyState(int key);
        [DllImport("kernel32.dll")]
        private static extern IntPtr GetStdHandle(int identifier);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool ReadFile(IntPtr handle, byte[] buffer, uint length,
                                            out uint read, IntPtr overlapped);

        internal Host(PumpWindow pump, ApplicationContext application)
        { this.pump = pump; this.application = application; }

        internal static Dictionary<string, object> Object(params object[] pairs)
        {
            var result = new Dictionary<string, object>();
            for (int index = 0; index < pairs.Length; index += 2)
                result[(string)pairs[index]] = pairs[index + 1];
            return result;
        }
        private static object Value(Dictionary<string, object> source, string key, object fallback = null)
        { object value; return source.TryGetValue(key, out value) ? value : fallback; }
        private static string Text(Dictionary<string, object> source, string key, int maximum = 1024, string fallback = null)
        {
            object raw = Value(source, key, fallback);
            var value = raw as string;
            if (value == null || value.Length > maximum || value.IndexOf('\0') >= 0) throw new HostError("INVALID_REQUEST");
            return value;
        }
        private static int Integer(Dictionary<string, object> source, string key, int fallback = 0)
        {
            object value = Value(source, key, fallback);
            if (!(value is int)) throw new HostError("INVALID_REQUEST");
            return (int)value;
        }
        private static bool Flag(Dictionary<string, object> source, string key, bool fallback = false)
        {
            object value = Value(source, key, fallback);
            if (!(value is bool)) throw new HostError("INVALID_REQUEST");
            return (bool)value;
        }
        private static double Number(Dictionary<string, object> source, string key, double fallback = 0)
        {
            object value = Value(source, key, fallback);
            if (!(value is int) && !(value is long) && !(value is double) && !(value is decimal))
                throw new HostError("INVALID_REQUEST");
            double number = Convert.ToDouble(value, CultureInfo.InvariantCulture);
            if (Double.IsNaN(number) || Double.IsInfinity(number)) throw new HostError("INVALID_REQUEST");
            return number;
        }
        private void Send(Dictionary<string, object> message)
        {
            try { lock (outputLock) { Console.Out.WriteLine(json.Serialize(message)); Console.Out.Flush(); } }
            catch (IOException) { pump.Post(delegate { BeginShutdown(); }); }
        }
        private void Event(PageState page, string name, params object[] fields)
        {
            if (page.Closed || closing) return;
            var message = Object("event", name, "page", page.Id);
            for (int index = 0; index < fields.Length; index += 2) message[(string)fields[index]] = fields[index + 1];
            Send(message);
        }

        internal void ReadInput()
        {
            var thread = new Thread(delegate()
            {
                try
                {
                    // Qt's Windows QProcess pipes must be read as ordinary
                    // Win32 pipe handles, not through .NET ConsoleStream.
                    var handle = GetStdHandle(-10);
                    var buffer = new byte[4096];
                    using (var frame = new MemoryStream())
                    {
                        uint count;
                        while (ReadFile(handle, buffer, (uint)buffer.Length, out count, IntPtr.Zero) && count > 0)
                        {
                            for (int index = 0; index < count; index++)
                            {
                                if (buffer[index] == 10)
                                {
                                    string captured = Encoding.UTF8.GetString(frame.ToArray()).TrimEnd('\r');
                                    frame.SetLength(0);
                                    pump.Post(delegate { HandleLine(captured); });
                                }
                                else
                                {
                                    frame.WriteByte(buffer[index]);
                                    if (frame.Length > 8 * 1024 * 1024) throw new IOException();
                                }
                            }
                        }
                    }
                }
                catch (IOException) { }
                pump.Post(delegate { BeginShutdown(); });
            });
            thread.IsBackground = true;
            thread.Name = "MediaFlow IPC";
            thread.Start();
        }

        private async void HandleLine(string line)
        {
            int id = -1;
            try
            {
                if (line.Length > 8 * 1024 * 1024) throw new HostError("REQUEST_TOO_LARGE");
                var request = json.DeserializeObject(line) as Dictionary<string, object>;
                if (request == null) throw new HostError("INVALID_REQUEST");
                id = Integer(request, "id", -1);
                if (id < 0) throw new HostError("INVALID_REQUEST");
                string op = Text(request, "op", 64);
                if (closing && op != "close") throw new HostError("HOST_CLOSING");
                object result = await Execute(op, request);
                Send(Object("id", id, "ok", true, "result", result));
                if (op == "close") pump.Post(delegate { application.ExitThread(); });
            }
            catch (HostError error) { Send(Object("id", id, "ok", false, "error", error.Code)); }
            catch (Exception error) { Send(Object("id", id, "ok", false, "error", "OPERATION_FAILED",
                "errorType", error.GetType().Name, "hresult", error.HResult)); }
        }

        private async Task<object> Execute(string op, Dictionary<string, object> request)
        {
            if (op == "initialize")
            {
                if (initialization != null) throw new HostError("ALREADY_INITIALIZED");
                initialization = Initialize(Text(request, "userDataFolder", 32768), Flag(request, "persistent"));
                return await initialization;
            }
            if (op == "close") { await Shutdown(); return true; }
            if (initialization == null) throw new HostError("NOT_INITIALIZED");
            await initialization;
            if (closing) throw new HostError("HOST_CLOSING");
            if (op == "close_page")
            {
                string pageId = Text(request, "page", 128);
                PageState close;
                if (pages.TryGetValue(pageId, out close)) { pages.Remove(pageId); ClosePage(close); }
                return true;
            }
            // Keep an idle profile idle until native clearing has completed.
            if (clearingProfile) throw new HostError("PROFILE_IN_USE");
            if (op == "clear_browsing_data") return await ClearBrowsingData();
            if (op == "create") return await CreatePage(request);
            if (op == "set_cookie") return await SetCookie(request);
            if (op == "get_cookies") return await GetCookies(request);
            PageState page;
            if (!pages.TryGetValue(Text(request, "page", 128), out page)) throw new HostError("PAGE_NOT_FOUND");
            if (op == "resize")
            {
                page.Width = Dimension(request, "width"); page.Height = Dimension(request, "height");
                if (page.Controller != null) SetBounds(page);
                return true;
            }
            if (op == "visible")
            {
                page.Visible = Flag(request, "value");
                if (page.Wrapper != null) page.Wrapper.Visible(page.Visible);
                if (page.Controller != null) page.Controller.IsVisible = page.Visible;
                return true;
            }
            await page.Ready.Task;
            if (page.Closed || closing) throw new HostError("PAGE_CLOSED");
            if (op == "navigate") { page.Core.Navigate(NavigationUrl(Text(request, "url", 1024 * 1024))); return true; }
            if (op == "stop") { page.Core.Stop(); return true; }
            if (op == "zoom")
            {
                double factor = Number(request, "factor", 1);
                if (factor < 0.25 || factor > 5) throw new HostError("INVALID_ZOOM");
                page.Controller.ZoomFactor = factor; return true;
            }
            if (op == "focus") { page.Controller.MoveFocus(CoreWebView2MoveFocusReason.Programmatic); return true; }
            if (op == "cdp") return await Cdp(page, request);
            if (op == "eval") return await Evaluate(page, Text(request, "script", 8 * 1024 * 1024), Integer(request, "world"));
            if (op == "add_script") return await page.Core.AddScriptToExecuteOnDocumentCreatedAsync(Text(request, "script", 8 * 1024 * 1024));
            throw new HostError("UNKNOWN_OPERATION");
        }

        private async Task<object> Initialize(string userDataFolder, bool persistentMode)
        {
            string folder, temporary;
            try { folder = Path.GetFullPath(userDataFolder); temporary = Path.GetFullPath(Path.GetTempPath()).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar; }
            catch (Exception) { throw new HostError("INVALID_PROFILE_FOLDER"); }
            if (persistentMode)
            {
                // Only the app's explicit local BrowserProfile is reusable;
                // never interpret a relative path or open a system browser UDF.
                if (userDataFolder.Length < 3 || !Char.IsLetter(userDataFolder[0]) ||
                    userDataFolder[1] != ':' || (userDataFolder[2] != '\\' && userDataFolder[2] != '/'))
                    throw new HostError("INVALID_PROFILE_FOLDER");
                folder = folder.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
                if (!String.Equals(Path.GetFileName(folder), "BrowserProfile", StringComparison.OrdinalIgnoreCase))
                    throw new HostError("INVALID_PROFILE_FOLDER");
            }
            else
            {
                if (!folder.StartsWith(temporary, StringComparison.OrdinalIgnoreCase) || folder.TrimEnd(Path.DirectorySeparatorChar) == temporary.TrimEnd(Path.DirectorySeparatorChar))
                    throw new HostError("INVALID_PROFILE_FOLDER");
                if (Directory.Exists(folder) && Directory.GetFileSystemEntries(folder).Length != 0)
                    throw new HostError("PROFILE_FOLDER_NOT_EMPTY");
            }
            Directory.CreateDirectory(folder);
            persistent = persistentMode;
            // Do not inherit environment overrides that can select a user's real
            // profile, introduce a debugging TCP port, or select another browser.
            Environment.SetEnvironmentVariable("WEBVIEW2_USER_DATA_FOLDER", null);
            Environment.SetEnvironmentVariable("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", null);
            Environment.SetEnvironmentVariable("WEBVIEW2_BROWSER_EXECUTABLE_FOLDER", null);
            var options = new CoreWebView2EnvironmentOptions();
            options.AllowSingleSignOnUsingOSPrimaryAccount = false;
            options.ExclusiveUserDataFolderAccess = true;
            environment = await CoreWebView2Environment.CreateAsync(null, folder, options);
            if (!String.Equals(Path.GetFullPath(environment.UserDataFolder), folder, StringComparison.OrdinalIgnoreCase))
                throw new HostError("PROFILE_FOLDER_OVERRIDDEN");
            environment.BrowserProcessExited += delegate { browserExited.TrySetResult(true); };
            keeper = await environment.CreateCoreWebView2ControllerAsync(new IntPtr(-3), ControllerOptions());
            keeper.IsVisible = false;
            keeper.Bounds = new Rectangle(0, 0, 1, 1);
            SecureSettings(keeper.CoreWebView2);
            if (keeper.CoreWebView2.Profile.IsInPrivateModeEnabled != !persistent)
                throw new HostError(persistent ? "PROFILE_NOT_PERSISTENT" : "PROFILE_NOT_PRIVATE");
            return Object("backend", "WebView2", "runtimeVersion", environment.BrowserVersionString,
                          "userAgent", keeper.CoreWebView2.Settings.UserAgent, "persistent", persistent);
        }
        private CoreWebView2ControllerOptions ControllerOptions()
        {
            var options = environment.CreateCoreWebView2ControllerOptions();
            options.ProfileName = "MediaFlow";
            options.IsInPrivateModeEnabled = !persistent;
            return options;
        }
        private async Task<object> ClearBrowsingData()
        {
            if (pages.Count != 0 || clearingProfile) throw new HostError("PROFILE_IN_USE");
            clearingProfile = true;
            try
            {
                await keeper.CoreWebView2.Profile.ClearBrowsingDataAsync(CoreWebView2BrowsingDataKinds.AllProfile);
                return true;
            }
            finally { clearingProfile = false; }
        }
        private static int Dimension(Dictionary<string, object> request, string key)
        {
            int dimension = Integer(request, key, 1);
            if (dimension < 0 || dimension > 32768) throw new HostError("INVALID_BOUNDS");
            return Math.Max(1, dimension);
        }
        private static void SetBounds(PageState page)
        {
            if (page.Wrapper != null) page.Wrapper.Resize(page.Width, page.Height);
            page.Controller.BoundsMode = CoreWebView2BoundsMode.UseRawPixels;
            page.Controller.Bounds = new Rectangle(0, 0, page.Width, page.Height);
            page.Controller.NotifyParentWindowPositionChanged();
        }
        private async Task<object> CreatePage(Dictionary<string, object> request)
        {
            string id = Text(request, "page", 128);
            if (String.IsNullOrEmpty(id) || pages.ContainsKey(id)) throw new HostError("PAGE_ALREADY_EXISTS");
            string parentText = Text(request, "parentHwnd", 32);
            long parentValue;
            bool valid = parentText.StartsWith("0x", StringComparison.OrdinalIgnoreCase)
                ? Int64.TryParse(parentText.Substring(2), NumberStyles.HexNumber, CultureInfo.InvariantCulture, out parentValue)
                : Int64.TryParse(parentText, NumberStyles.Integer, CultureInfo.InvariantCulture, out parentValue);
            if (!valid || parentValue <= 0 || !IsWindow(new IntPtr(parentValue))) throw new HostError("INVALID_PARENT_HWND");
            var page = new PageState { Id = id, Width = Dimension(request, "width"),
                Height = Dimension(request, "height"), Visible = Flag(request, "visible", true) };
            pages.Add(id, page); // Register before awaiting: close_page can cancel a pending create.
            try
            {
                page.Wrapper = new ChildWindow(page.Width, page.Height);
                page.Controller = await environment.CreateCoreWebView2ControllerAsync(page.Wrapper.Handle, ControllerOptions());
                PageState active;
                if (page.Closed || closing || !pages.TryGetValue(id, out active) || !System.Object.ReferenceEquals(active, page))
                { page.Controller.Close(); throw new HostError("PAGE_CLOSED"); }
                page.Core = page.Controller.CoreWebView2;
                page.Controller.IsVisible = false;
                page.Wrapper.Attach(new IntPtr(parentValue));
                SecureSettings(page.Core);
                Subscribe(page);
                SetBounds(page);
                page.Wrapper.Visible(page.Visible);
                page.Controller.IsVisible = page.Visible;
                page.Ready.TrySetResult(page);
                return Object("userAgent", page.Core.Settings.UserAgent, "runtimeVersion", environment.BrowserVersionString,
                              "persistent", persistent);
            }
            catch (Exception)
            {
                PageState active;
                if (pages.TryGetValue(id, out active) && System.Object.ReferenceEquals(active, page)) pages.Remove(id);
                ClosePage(page);
                page.Ready.TrySetException(new HostError("PAGE_CLOSED"));
                throw;
            }
        }
        private static void SecureSettings(CoreWebView2 core)
        {
            core.Settings.AreHostObjectsAllowed = false;
            core.Settings.IsWebMessageEnabled = false;
            core.Settings.AreDevToolsEnabled = false;
            core.Settings.IsPasswordAutosaveEnabled = false;
            core.Settings.IsGeneralAutofillEnabled = false;
            core.Settings.AreDefaultScriptDialogsEnabled = false;
            core.ScriptDialogOpening += delegate(object sender, CoreWebView2ScriptDialogOpeningEventArgs args) { args.Accept(); };
            core.DownloadStarting += delegate(object sender, CoreWebView2DownloadStartingEventArgs args) { args.Cancel = true; args.Handled = true; };
            core.LaunchingExternalUriScheme += delegate(object sender, CoreWebView2LaunchingExternalUriSchemeEventArgs args) { args.Cancel = true; };
            core.NewWindowRequested += delegate(object sender, CoreWebView2NewWindowRequestedEventArgs args)
            {
                args.Handled = true;
                Uri uri;
                if (Uri.TryCreate(args.Uri, UriKind.Absolute, out uri) && (uri.Scheme == "http" || uri.Scheme == "https"))
                    core.Navigate(args.Uri);
            };
        }
        private void Subscribe(PageState page)
        {
            page.Controller.AcceleratorKeyPressed += delegate(object sender, CoreWebView2AcceleratorKeyPressedEventArgs args)
            {
                if (args.KeyEventKind != CoreWebView2KeyEventKind.KeyDown &&
                    args.KeyEventKind != CoreWebView2KeyEventKind.SystemKeyDown) return;
                int key = (int)args.VirtualKey;
                bool control = GetKeyState(0x11) < 0;
                bool alt = GetKeyState(0x12) < 0;
                string shortcut = null;
                if (key == 27) shortcut = "escape";
                else if (control && !alt)
                {
                    if (key == 187 || key == 107) shortcut = "zoom_in";
                    else if (key == 189 || key == 109) shortcut = "zoom_out";
                    else if (key == 48 || key == 96) shortcut = "zoom_reset";
                }
                if (shortcut != null)
                {
                    args.Handled = shortcut != "escape";
                    Event(page, "shortcut", "key", shortcut);
                }
            };
            page.Core.NavigationStarting += delegate
            {
                page.Generation++; page.Worlds.Clear(); Event(page, "load_started");
            };
            page.Core.SourceChanged += delegate { Event(page, "url_changed", "url", page.Core.Source); };
            page.Core.NavigationCompleted += delegate(object sender, CoreWebView2NavigationCompletedEventArgs args)
            { Event(page, "load_finished", "success", args.IsSuccess); };
            page.Core.ProcessFailed += delegate { page.Worlds.Clear(); Event(page, "process_failed"); };
            foreach (string eventName in CdpEvents)
            {
                string name = eventName;
                var receiver = page.Core.GetDevToolsProtocolEventReceiver(name);
                receiver.DevToolsProtocolEventReceived += delegate(object sender, CoreWebView2DevToolsProtocolEventReceivedEventArgs args)
                {
                    if (page.Closed || closing) return;
                    try
                    {
                        var message = Object("method", name, "params", json.DeserializeObject(args.ParameterObjectAsJson));
                        if (!String.IsNullOrEmpty(args.SessionId)) message["sessionId"] = args.SessionId;
                        if (name == "Runtime.executionContextsCleared" && String.IsNullOrEmpty(args.SessionId)) page.Worlds.Clear();
                        Event(page, "cdp", "message", message);
                    }
                    catch (Exception) { /* No raw protocol or page log. */ }
                };
                page.Receivers.Add(receiver);
            }
        }
        private async Task<object> Cdp(PageState page, Dictionary<string, object> request)
        {
            var message = Value(request, "message") as Dictionary<string, object>;
            if (message == null) throw new HostError("INVALID_REQUEST");
            int id = Integer(message, "id", -1);
            if (id < 0) throw new HostError("INVALID_REQUEST");
            string method = Text(message, "method", 256);
            var parameters = Value(message, "params", new Dictionary<string, object>()) as Dictionary<string, object>;
            if (parameters == null) throw new HostError("INVALID_REQUEST");
            string session = Text(message, "sessionId", 1024, "");
            try
            {
                string result = String.IsNullOrEmpty(session)
                    ? await page.Core.CallDevToolsProtocolMethodAsync(method, json.Serialize(parameters))
                    : await page.Core.CallDevToolsProtocolMethodForSessionAsync(session, method, json.Serialize(parameters));
                return Object("id", id, "result", json.DeserializeObject(result));
            }
            catch (Exception) { return Object("id", id, "error", Object("message", "CDP_METHOD_FAILED")); }
        }
        private async Task<object> Evaluate(PageState page, string script, int world)
        {
            var parameters = Object("expression", script, "awaitPromise", true, "returnByValue", true);
            if (world != 0)
            {
                int context;
                if (!page.Worlds.TryGetValue(world, out context))
                {
                    long generation = page.Generation;
                    var tree = json.DeserializeObject(await page.Core.CallDevToolsProtocolMethodAsync("Page.getFrameTree", "{}")) as Dictionary<string, object>;
                    var frameTree = Value(tree, "frameTree") as Dictionary<string, object>;
                    var frame = Value(frameTree, "frame") as Dictionary<string, object>;
                    var isolated = json.DeserializeObject(await page.Core.CallDevToolsProtocolMethodAsync("Page.createIsolatedWorld",
                        json.Serialize(Object("frameId", Text(frame, "id"), "worldName", "MediaFlow." + world.ToString(CultureInfo.InvariantCulture),
                                              "grantUniveralAccess", false)))) as Dictionary<string, object>;
                    if (page.Generation != generation || page.Closed) throw new HostError("NAVIGATION_CHANGED");
                    context = Integer(isolated, "executionContextId");
                    page.Worlds[world] = context;
                }
                parameters["contextId"] = context;
            }
            var result = json.DeserializeObject(await page.Core.CallDevToolsProtocolMethodAsync("Runtime.evaluate", json.Serialize(parameters))) as Dictionary<string, object>;
            if (result == null || result.ContainsKey("exceptionDetails")) throw new HostError("SCRIPT_FAILED");
            var remote = Value(result, "result") as Dictionary<string, object>;
            return remote == null ? null : Value(remote, "value");
        }
        private static string NavigationUrl(string url)
        {
            Uri uri;
            if (url == "about:blank") return url;
            if (Uri.TryCreate(url, UriKind.Absolute, out uri) && (uri.Scheme == "http" || uri.Scheme == "https")) return url;
            throw new HostError("INVALID_URL");
        }
        private async Task<object> SetCookie(Dictionary<string, object> request)
        {
            var manager = keeper.CoreWebView2.CookieManager;
            var cookie = manager.CreateCookie(Text(request, "name", 1024), Text(request, "value", 32768),
                                              Text(request, "domain", 1024), Text(request, "path", 4096, "/"));
            cookie.IsSecure = Flag(request, "secure"); cookie.IsHttpOnly = Flag(request, "httpOnly");
            string sameSite = Text(request, "sameSite", 16, "Lax");
            if (String.Equals(sameSite, "None", StringComparison.OrdinalIgnoreCase)) cookie.SameSite = CoreWebView2CookieSameSiteKind.None;
            else if (String.Equals(sameSite, "Strict", StringComparison.OrdinalIgnoreCase)) cookie.SameSite = CoreWebView2CookieSameSiteKind.Strict;
            else if (String.Equals(sameSite, "Lax", StringComparison.OrdinalIgnoreCase)) cookie.SameSite = CoreWebView2CookieSameSiteKind.Lax;
            else throw new HostError("INVALID_COOKIE");
            if (Flag(request, "session", true)) cookie.Expires = DateTime.MinValue;
            else
            {
                double expires = Number(request, "expires");
                if (expires < 0 || expires > 253402300799) throw new HostError("INVALID_COOKIE");
                cookie.Expires = Epoch.AddSeconds(expires);
            }
            manager.AddOrUpdateCookie(cookie);
            foreach (var stored in await manager.GetCookiesAsync(""))
                if (stored.Name == cookie.Name && stored.Domain == cookie.Domain && stored.Path == cookie.Path && stored.Value == cookie.Value)
                    return true;
            return false;
        }
        private async Task<object> GetCookies(Dictionary<string, object> request)
        {
            string url = NavigationUrl(Text(request, "url", 1024 * 1024));
            if (url == "about:blank") throw new HostError("INVALID_URL");
            var result = new List<object>();
            foreach (var cookie in await keeper.CoreWebView2.CookieManager.GetCookiesAsync(url))
                result.Add(Object("name", cookie.Name, "value", cookie.Value, "domain", cookie.Domain, "path", cookie.Path,
                    "secure", cookie.IsSecure, "httpOnly", cookie.IsHttpOnly, "session", cookie.IsSession,
                    "expires", cookie.IsSession ? (object)null : Math.Floor((cookie.Expires.ToUniversalTime() - Epoch).TotalSeconds),
                    "sameSite", cookie.SameSite.ToString()));
            return result;
        }
        private static void ClosePage(PageState page)
        {
            page.Closed = true;
            page.Worlds.Clear();
            page.Ready.TrySetException(new HostError("PAGE_CLOSED"));
            if (page.Controller != null) { try { page.Controller.Close(); } catch (Exception) { } }
            if (page.Wrapper != null) { try { page.Wrapper.Dispose(); } catch (Exception) { } page.Wrapper = null; }
            page.Receivers.Clear();
        }
        private Task Shutdown()
        {
            if (shutdown == null) shutdown = ShutdownCore();
            return shutdown;
        }
        private async Task ShutdownCore()
        {
            closing = true;
            foreach (var page in pages.Values) ClosePage(page);
            pages.Clear();
            if (initialization != null && !initialization.IsCompleted)
                await Task.WhenAny(initialization, Task.Delay(10000));
            if (keeper != null) { try { keeper.Close(); } catch (Exception) { } keeper = null; }
            if (environment != null) await Task.WhenAny(browserExited.Task, Task.Delay(3000));
            environment = null;
        }
        private async void BeginShutdown()
        {
            try { await Shutdown(); }
            catch (Exception) { }
            application.ExitThread();
        }
        internal void Fail(Exception error)
        {
            Send(Object("id", -1, "ok", false, "error", "MESSAGE_PUMP_FAILED",
                        "errorType", error.GetType().Name, "hresult", error.HResult));
            foreach (var page in pages.Values) Event(page, "process_failed");
            BeginShutdown();
        }
    }

    internal static class Program
    {
        [DllImport("user32.dll")]
        private static extern bool SetProcessDpiAwarenessContext(IntPtr context);
        [DllImport("user32.dll")]
        private static extern bool SetProcessDPIAware();
        [STAThread]
        private static int Main()
        {
            try
            {
                try { SetProcessDpiAwarenessContext(new IntPtr(-4)); }
                catch (EntryPointNotFoundException) { SetProcessDPIAware(); }
                Console.SetOut(new StreamWriter(Console.OpenStandardOutput(), new UTF8Encoding(false)) { AutoFlush = true });
                Application.SetUnhandledExceptionMode(UnhandledExceptionMode.CatchException);
                using (var pump = new PumpWindow())
                using (var application = new ApplicationContext())
                {
                    SynchronizationContext.SetSynchronizationContext(new PumpContext(pump));
                    var host = new Host(pump, application);
                    Application.ThreadException += delegate(object sender, ThreadExceptionEventArgs args) { host.Fail(args.Exception); };
                    host.ReadInput();
                    Application.Run(application);
                }
                return 0;
            }
            catch (Exception error)
            {
                try { Console.Out.WriteLine(new JavaScriptSerializer().Serialize(Host.Object("id", -1, "ok", false,
                    "error", "STARTUP_FAILED", "errorType", error.GetType().Name, "hresult", error.HResult))); Console.Out.Flush(); }
                catch (Exception) { }
                return 1;
            }
        }
    }
}
