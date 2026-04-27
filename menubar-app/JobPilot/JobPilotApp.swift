import SwiftUI

@main
struct JobPilotApp: App {
    @StateObject private var state = AppState()
    /// AppDelegate handles the clean shutdown handshake on Cmd-Q / Force Quit
    /// / log-out, signals we can't reach from a pure-SwiftUI App scene.
    @NSApplicationDelegateAdaptor(JobPilotAppDelegate.self) private var appDelegate

    var body: some Scene {
        MenuBarExtra(state.isRunning ? "▶ \(state.todayCount)" : "⏸ \(state.todayCount)", systemImage: "paperplane") {
            MenuBarLauncherView()
                .environmentObject(state)
                .task {
                    // First view to appear: wire AppDelegate → AppState so
                    // shutdownAndQuit can find it on Cmd-Q.
                    appDelegate.state = state
                }
        }
        .menuBarExtraStyle(.window)

        Window("JobPilot", id: "control") {
            MenuBarView()
                .environmentObject(state)
                .task { appDelegate.state = state }
        }
        .defaultSize(width: 560, height: 760)

        Window("History", id: "history") {
            HistoryWindow()
                .environmentObject(state)
                .task { appDelegate.state = state }
        }
        .defaultSize(width: 980, height: 680)
    }
}

/// Routes Cmd-Q / Force Quit / log-out through `AppState.shutdownAndQuit` so
/// the backend process is always SIGTERM'd cleanly and the localhost port is
/// freed before we let AppKit terminate the GUI.
///
/// Clean STARTUP is handled inside `AppState.init()` itself — it spawns its
/// own bootstrap Task so the backend launch starts the moment the
/// `@StateObject` is created, before any view appears. We don't drive that
/// from this delegate to avoid a race where the AppDelegate fires before any
/// scene has had a chance to wire `state`.
final class JobPilotAppDelegate: NSObject, NSApplicationDelegate {
    weak var state: AppState?
    private var didStartShutdown = false

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard !didStartShutdown, let state else { return .terminateNow }
        didStartShutdown = true
        Task { @MainActor in
            // Pass replyToTerminate so shutdownAndQuit calls
            // NSApp.reply(toApplicationShouldTerminate:) instead of bouncing
            // back through NSApp.terminate again.
            await state.shutdownAndQuit(replyToTerminate: true)
        }
        return .terminateLater
    }
}

struct MenuBarLauncherView: View {
    @EnvironmentObject private var state: AppState
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text("JobPilot")
                        .font(.headline)
                    Text(state.statusHeadline)
                        .font(.caption)
                        .foregroundStyle(statusColor)
                }
                Spacer()
                Text(state.isRunning ? "Running live" : "Idle")
                    .font(.caption2)
                    .bold()
                    .padding(.horizontal, 8)
                    .padding(.vertical, 4)
                    .background(statusColor.opacity(0.14))
                    .foregroundStyle(statusColor)
                    .clipShape(RoundedRectangle(cornerRadius: 8))
            }

            if state.alarmIsRinging {
                Label("Action needed", systemImage: "bell.and.waves.left.and.right")
                    .font(.caption)
                    .foregroundStyle(.orange)
            }

            HStack {
                LauncherCounter(label: "Today", value: state.todayCount)
                LauncherCounter(label: "Week", value: state.status.week)
                LauncherCounter(label: "All", value: state.status.allTime)
            }

            Button("Open JobPilot Controls") {
                openWindow(id: "control")
                NSApp.activate(ignoringOtherApps: true)
            }
            .keyboardShortcut(.defaultAction)

            HStack {
                Button("Refresh") {
                    Task { await state.refresh() }
                }
                // Clean quit: stops any running run, kills the backend
                // process, sweeps the port, then quits the app.
                Button(role: .destructive) {
                    Task { await state.shutdownAndQuit() }
                } label: {
                    Text("Quit JobPilot")
                }
                .keyboardShortcut("q", modifiers: [.command])
            }
            .font(.caption)
        }
        .padding()
        .frame(width: 320)
        .task {
            state.startHealthMonitor()
            state.startDashboardMonitor()
            state.startStream()
            await state.refresh()
        }
    }

    private var statusColor: Color {
        if state.status.state.contains("error") || state.status.state.contains("offline") || state.status.state.contains("failed") {
            return .red
        }
        if state.isRunning {
            return .green
        }
        if state.alarmIsRinging {
            return .orange
        }
        return .secondary
    }
}

private struct LauncherCounter: View {
    let label: String
    let value: Int

    var body: some View {
        VStack(spacing: 2) {
            Text("\(value)")
                .font(.headline)
            Text(label)
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
    }
}
