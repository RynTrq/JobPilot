import SwiftUI

struct MenuBarView: View {
    @EnvironmentObject private var state: AppState
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            header

            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    if !state.backendReachable {
                        backendUnreachableBanner
                    }
                    controlPanel
                    pendingActions
                    runSummary
                    currentWork
                    quickAccess
                    recentEvents
                }
                .padding(.trailing, 4)
            }
        }
        .padding()
        .frame(width: 520, height: 700)
        .task {
            state.startHealthMonitor()
            state.startDashboardMonitor()
            state.startStream()
            await state.refresh()
        }
        .sheet(isPresented: $state.showAlarmWindow) {
            AlarmWindow()
                .environmentObject(state)
        }
        .sheet(isPresented: $state.showClassifierReview) {
            ClassifierReviewWindow()
                .environmentObject(state)
        }
        .sheet(isPresented: $state.showManualTakeover) {
            ManualTakeoverWindow()
                .id(state.manualTakeover?.token)
                .environmentObject(state)
        }
        .sheet(isPresented: $state.showApproval) {
            ApprovalWindow()
                .environmentObject(state)
        }
        .sheet(item: $state.selectedApplication) { item in
            ApplicationDetailWindow(item: item)
                .environmentObject(state)
        }
        .sheet(isPresented: $state.showConsole) {
            ConsoleLogsWindow()
                .environmentObject(state)
        }
        .sheet(isPresented: $state.showAlarmSettings) {
            AlarmSettingsWindow()
                .environmentObject(state)
        }
    }

    private var header: some View {
        HStack(alignment: .center) {
            VStack(alignment: .leading, spacing: 2) {
                Text("JobPilot")
                    .font(.headline)
                Text(state.statusHeadline)
                    .font(.caption)
                    .foregroundStyle(statusColor)
            }
            Spacer()
            Button("Refresh") {
                Task { await state.refresh() }
            }
            .font(.caption)
            .help(state.backendReachable ? "Reload the latest backend status and history." : "Backend is offline. Refresh will retry the local backend connection.")
            Button {
                state.showAlarmSettings = true
            } label: {
                Image(systemName: "gearshape")
            }
            .buttonStyle(.plain)
            .help("Open settings and alarm controls.")
        }
    }

    /// Big red banner shown whenever the /health probe fails. Previously the UI happily
    /// displayed stale state when the backend process was dead, so users thought the app
    /// was still working. This is the single most important visual signal during outages.
    private var backendUnreachableBanner: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "bolt.slash.fill")
                .font(.title3)
                .foregroundStyle(.red)
            VStack(alignment: .leading, spacing: 4) {
                Text("Backend not reachable")
                    .font(.subheadline).bold()
                    .foregroundStyle(.red)
                Text("The local JobPilot backend isn't responding on \(state.backendEndpoint). JobPilot will try to start it automatically, clear a stuck port, or switch to a nearby open port.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                HStack {
                    Button("Refresh") { Task { await state.refresh() } }
                    Button("Open Logs") { state.showConsole = true }
                }
                .font(.caption)
            }
            Spacer(minLength: 0)
        }
        .padding(10)
        .background(Color.red.opacity(0.10))
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .stroke(Color.red.opacity(0.35), lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    private var controlPanel: some View {
        InfoSection(title: "Run Control") {
            VStack(alignment: .leading, spacing: 8) {
                TextField("Career page or job URL", text: $state.careerURL)
                    .textFieldStyle(.roundedBorder)

                VStack(alignment: .leading, spacing: 6) {
                    Toggle("Limit this run", isOn: $state.dailyLimitEnabled)
                        .toggleStyle(.switch)

                    if state.dailyLimitEnabled {
                        HStack(spacing: 8) {
                            Text("Daily limit")
                            TextField("25", value: $state.dailyLimit, format: .number)
                                .textFieldStyle(.roundedBorder)
                                .frame(width: 76)
                                .onSubmit {
                                    state.normalizeDailyLimit()
                                }
                            Text("jobs")
                                .foregroundStyle(.secondary)
                            Spacer()
                        }
                    } else {
                        Text("No daily limit")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
                .help("When enabled, JobPilot stops this run after the typed number of jobs. When disabled, no per-run limit is sent.")

                HStack {
                    Button(state.isRunning ? "Stop Run" : "Start Run") {
                        Task {
                            if state.isRunning {
                                await state.stop()
                            } else {
                                await state.start()
                            }
                        }
                    }
                    .keyboardShortcut(.defaultAction)
                    .disabled(startStopDisabled)
                    .help(startStopHelp)

                    Spacer()

                    StatusPill(text: runningStatusText, color: statusColor)
                }
                HStack(spacing: 12) {
                    Toggle("Dry run", isOn: Binding(
                        get: { state.settings.dryRun },
                        set: { dryRun in Task { await state.setLiveSubmit(!dryRun) } }
                    ))
                    .toggleStyle(.switch)
                    .foregroundStyle(state.settings.dryRun ? Color.green : Color.secondary)
                    Image(systemName: "info.circle")
                        .foregroundStyle(.secondary)
                        .help("Dry run skips final submits. Turning on Real submit turns Dry run off.")
                    Spacer()
                    Toggle("Real submit", isOn: Binding(
                        get: { state.settings.liveSubmitEnabled },
                        set: { enabled in Task { await state.setLiveSubmit(enabled) } }
                    ))
                    .toggleStyle(.switch)
                    .foregroundStyle(state.settings.liveSubmitEnabled ? Color.red : Color.secondary)
                }
                VStack(alignment: .leading, spacing: 4) {
                    Toggle("Final review required", isOn: Binding(
                        get: { state.settings.finalReviewRequired },
                        set: { enabled in Task { await state.setFinalReviewRequired(enabled) } }
                    ))
                    .toggleStyle(.switch)
                    .foregroundStyle(state.settings.finalReviewRequired ? Color.orange : Color.secondary)
                    Text(state.settings.finalReviewRequired ? "Each job pauses and rings the alarm before the final submit/dry-run step." : "Clean jobs skip final review. Missing fields, warnings, login, captcha, or errors still pause.")
                        .font(.caption2)
                        .foregroundStyle(state.settings.finalReviewRequired ? Color.orange : Color.secondary)
                }
                VStack(alignment: .leading, spacing: 4) {
                    Toggle("Auto-classify (no review needed)", isOn: Binding(
                        get: { state.settings.classifierAutoPassWhenAboveThreshold },
                        set: { enabled in Task { await state.setClassifierAutoPass(enabled) } }
                    ))
                    .toggleStyle(.switch)
                    .foregroundStyle(state.settings.classifierAutoPassWhenAboveThreshold ? Color.orange : Color.secondary)
                    Text("When ON, the classifier's verdict is final. When OFF, each pass is confirmed and the answer trains the classifier.")
                        .font(.caption2)
                        .foregroundStyle(state.settings.classifierAutoPassWhenAboveThreshold ? Color.orange : Color.secondary)
                }
                VStack(alignment: .leading, spacing: 4) {
                    Toggle("Watch browser", isOn: Binding(
                        get: { state.settings.liveModeEnabled },
                        set: { enabled in Task { await state.setLiveMode(enabled) } }
                    ))
                    .toggleStyle(.switch)
                    .foregroundStyle(state.settings.liveModeEnabled ? Color.blue : Color.secondary)
                    Text(state.settings.liveModeEnabled
                         ? "The automation browser stays visible and comes to the front while JobPilot works. When the app pauses, type directly into that browser window, then press \"Use browser input\"."
                         : "Turn this on to watch each step live in the automation browser and take over when needed.")
                        .font(.caption2)
                        .foregroundStyle(state.settings.liveModeEnabled ? Color.blue : Color.secondary)
                }
                if state.settings.liveModeEnabled && state.isRunning {
                    Button("Show Automation Browser") {
                        Task { await state.focusAutomationBrowser() }
                    }
                    .font(.caption)
                    .help("Bring the single live automation browser window to the front.")
                }
                if state.alarmIsRinging {
                    HStack(spacing: 8) {
                        Label("Alarm ringing: action needed", systemImage: "bell.and.waves.left.and.right")
                            .font(.caption)
                            .foregroundStyle(.orange)
                        AlarmLevelBadge(level: state.alarmLevelLabel, snoozed: state.alarmIsSnoozed)
                        Spacer()
                        Button(state.alarmIsSnoozed ? "Snoozed" : "Snooze") {
                            state.snoozeAlarm()
                        }
                        .disabled(state.alarmIsSnoozed)
                        .font(.caption)
                        Button("Silence") {
                            state.stopAlarm()
                        }
                        .font(.caption)
                    }
                }
            }
        }
    }

    private var pendingActions: some View {
        let actions = pendingActionRows
        return Group {
            if !actions.isEmpty {
                InfoSection(title: "Needs Your Input") {
                    VStack(alignment: .leading, spacing: 8) {
                        ForEach(actions) { action in
                            HStack(alignment: .top, spacing: 8) {
                                Image(systemName: action.icon)
                                    .foregroundStyle(.orange)
                                    .frame(width: 18)
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(action.title)
                                        .font(.caption)
                                        .bold()
                                    Text(action.detail)
                                        .font(.caption2)
                                        .foregroundStyle(.secondary)
                                        .lineLimit(2)
                                }
                                Spacer()
                                switch action.kind {
                                case .classifierReview:
                                    Button("Open") { state.openClassifierReview() }.font(.caption)
                                case .approval:
                                    Button("Open") { state.openApproval() }.font(.caption)
                                case .manualTakeover:
                                    Button("Open") { state.openManualTakeover() }.font(.caption)
                                case .alarm:
                                    Button("Open") { state.openAlarm() }.font(.caption)
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    private var runSummary: some View {
        InfoSection(title: "Run Summary") {
            VStack(alignment: .leading, spacing: 10) {
                HStack {
                    Counter(label: "Today", value: state.todayCount)
                    Counter(label: "Week", value: state.status.week)
                    Counter(label: "All", value: state.status.allTime)
                    Counter(label: "Needs Attention", value: state.activity.filter(\.needsAttention).count)
                }

                HStack {
                    SummaryChip(label: "Prepared", value: state.activity.filter(\.prepared).count, color: .blue)
                    SummaryChip(label: "Needs Review", value: state.activity.filter(\.needsAttention).count, color: .orange)
                    SummaryChip(label: "Filtered", value: state.activity.filter { $0.decision == "fail" || $0.decision == "human_fail" }.count, color: .gray)
                    Spacer()
                }
            }
        }
    }

    private var currentWork: some View {
        InfoSection(title: "Current Work") {
            VStack(alignment: .leading, spacing: 6) {
                Label(state.status.currentJob ?? "No active job", systemImage: state.isRunning ? "gearshape.2" : "pause.circle")
                    .font(.caption)
                    .lineLimit(3)
                if let stageMessage = state.status.currentStageMessage, !stageMessage.isEmpty, state.isRunning {
                    Label(stageMessage, systemImage: stageIcon(for: state.status.currentStage))
                        .font(.caption)
                        .foregroundStyle(.blue)
                        .lineLimit(3)
                }
                Label(state.lastEventText, systemImage: "dot.radiowaves.left.and.right")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(3)
            }
        }
    }

    private func stageIcon(for stage: String?) -> String {
        switch stage {
        case "opening_browser": return "safari"
        case "listing_jobs", "listed_jobs": return "list.bullet.rectangle"
        case "parsing_job_description": return "doc.text.magnifyingglass"
        case "classifying_job": return "brain"
        case "classifier_review": return "person.fill.questionmark"
        case "building_resume_context": return "wand.and.stars"
        case "generating_resume": return "doc.richtext"
        case "generating_cover_letter": return "text.quote"
        case "opening_application": return "rectangle.and.pencil.and.ellipsis"
        case "enumerating_fields", "enumerated_fields": return "list.bullet.clipboard"
        case "answering_fields", "answering_field": return "text.cursor"
        case "awaiting_approval": return "hand.thumbsup"
        case "filling_form", "filling_field": return "keyboard"
        case "uploading_resume", "uploading_cover_letter": return "paperclip"
        case "submitting": return "paperplane"
        default: return "gearshape.2"
        }
    }

    private var quickAccess: some View {
        InfoSection(title: "Quick Access") {
            HStack {
                Button("Console") {
                    state.showConsole = true
                    Task { await state.refreshLogs() }
                }
                Button("History") { openWindow(id: "history") }
                Button("Alarm Settings") { state.showAlarmSettings = true }
                Spacer()
            }
            .font(.caption)
        }
    }

    private var startStopDisabled: Bool {
        if state.isRunning {
            return false
        }
        return state.careerURL.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    private var startStopHelp: String {
        if state.isRunning {
            return "Request a graceful stop after the current browser step finishes."
        }
        if state.careerURL.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return "Paste a careers page or job URL before starting."
        }
        return "Start a single automation run using the current browser session."
    }

    private var recentEvents: some View {
        InfoSection(title: "Live Events") {
            VStack(alignment: .leading, spacing: 8) {
                if state.events.isEmpty {
                    Text("No live events yet.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else {
                    ForEach(state.events.prefix(8)) { event in
                        TimelineRow(event: event)
                    }
                    HStack {
                        Spacer()
                        Button("Clear Events") {
                            state.clearEvents()
                        }
                        .font(.caption)
                    }
                }
            }
        }
    }

    private var runningStatusText: String {
        if state.isRunning {
            if let stage = state.status.currentStage, !stage.isEmpty {
                return stage.replacingOccurrences(of: "_", with: " ").capitalized
            }
            return "Running"
        }
        return state.status.state.capitalized
    }

    private var statusColor: Color {
        if state.status.state.contains("error") || state.status.state.contains("offline") || state.status.state.contains("failed") {
            return .red
        }
        if state.isRunning {
            return .green
        }
        if state.status.state.contains("starting") {
            return .orange
        }
        return .secondary
    }

    private var pendingActionRows: [PendingActionRow] {
        var rows: [PendingActionRow] = []
        if let review = state.classifierReview {
            rows.append(PendingActionRow(kind: .classifierReview, icon: "line.3.horizontal.decrease.circle", title: "Classifier review", detail: review.title ?? review.jobURL ?? "Review whether this passed job should continue."))
        }
        if let approval = state.approval {
            rows.append(PendingActionRow(kind: .approval, icon: "checkmark.seal", title: "Final approval", detail: approval.title ?? approval.jobURL ?? "Review documents, field answers, and submit/skip."))
        }
        if let alarm = state.alarm {
            rows.append(PendingActionRow(kind: .alarm, icon: "questionmark.circle", title: "Missing answer", detail: alarm.question))
        }
        if let manual = state.manualTakeover {
            rows.append(PendingActionRow(kind: .manualTakeover, icon: "hand.raised", title: "Manual browser takeover", detail: manual.reason ?? manual.currentURL ?? manual.jobURL ?? "Finish login/captcha in the visible browser."))
        }
        return rows
    }
}

private struct Counter: View {
    let label: String
    let value: Int

    var body: some View {
        VStack {
            Text("\(value)").font(.headline)
            Text(label).font(.caption2)
        }
        .frame(maxWidth: .infinity)
    }
}

private struct InfoSection<Content: View>: View {
    let title: String
    @ViewBuilder let content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title)
                .font(.caption)
                .bold()
                .foregroundStyle(.secondary)
            content
        }
        .padding(10)
        .background(Color.gray.opacity(0.07))
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }
}

private struct StatusPill: View {
    let text: String
    let color: Color

    var body: some View {
        Text(text)
            .font(.caption2)
            .bold()
            .padding(.horizontal, 8)
            .padding(.vertical, 4)
            .background(color.opacity(0.14))
            .foregroundStyle(color)
            .clipShape(RoundedRectangle(cornerRadius: 8))
    }
}

private struct SummaryChip: View {
    let label: String
    let value: Int
    let color: Color

    var body: some View {
        Text("\(label): \(value)")
            .font(.caption2)
            .padding(.horizontal, 8)
            .padding(.vertical, 4)
            .background(color.opacity(0.12))
            .clipShape(RoundedRectangle(cornerRadius: 8))
    }
}

private struct TimelineRow: View {
    let event: TimelineEvent

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: icon)
                .foregroundStyle(color)
                .frame(width: 18)
            VStack(alignment: .leading, spacing: 2) {
                HStack {
                    Text(event.title)
                        .font(.caption)
                        .bold()
                    Spacer()
                    Text(event.timeText)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
                if let detail = event.detail, !detail.isEmpty {
                    Text(detail)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .lineLimit(3)
                        .textSelection(.enabled)
                }
            }
        }
    }

    private var icon: String {
        switch event.level {
        case .success: return "checkmark.circle"
        case .warning: return "exclamationmark.triangle"
        case .error: return "xmark.octagon"
        case .info: return "info.circle"
        }
    }

    private var color: Color {
        switch event.level {
        case .success: return .green
        case .warning: return .orange
        case .error: return .red
        case .info: return .blue
        }
    }
}

private struct PendingActionRow: Identifiable {
    enum Kind {
        case classifierReview
        case approval
        case alarm
        case manualTakeover
    }

    let id = UUID()
    let kind: Kind
    let icon: String
    let title: String
    let detail: String
}

struct ApplicationDetailWindow: View {
    @EnvironmentObject private var state: AppState
    @Environment(\.dismiss) private var dismiss
    let item: ApplicationRow
    @State private var showDeleteConfirmation = false

    private var retryMode: HistorySectionMode {
        state.settings.liveSubmitEnabled ? .realSubmit : .dryRun
    }

    private var retryLockedInCurrentMode: Bool {
        switch retryMode {
        case .dryRun: return item.dryRunSucceeded
        case .realSubmit: return item.realSubmitSucceeded
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(item.title ?? "Untitled role")
                .font(.title3)
                .bold()
            Text(item.displayCompany)
                .foregroundStyle(.secondary)
            Text(item.jobURL)
                .font(.caption)
                .textSelection(.enabled)
                .foregroundStyle(.secondary)

            Divider()

            Grid(alignment: .leading, horizontalSpacing: 16, verticalSpacing: 8) {
                DetailLine(label: "Status", value: item.displayStatus)
                if let appliedAt = item.appliedAt {
                    DetailLine(label: "Application Date", value: appliedAt)
                }
                if let location = item.location, !location.isEmpty {
                    DetailLine(label: "Location", value: location)
                }
                if let applicationType = item.applicationType, !applicationType.isEmpty {
                    DetailLine(label: "Application Type", value: applicationType)
                }
            }

            if let error = item.error, !error.isEmpty {
                Divider()
                Text("Error")
                    .font(.caption)
                    .bold()
                Text(error)
                    .font(.caption)
                    .foregroundStyle(.orange)
                    .textSelection(.enabled)
            }

            if let latex = item.resumeLatexCode, !latex.isEmpty {
                Divider()
                VStack(alignment: .leading, spacing: 8) {
                    Text("Resume LaTeX")
                        .font(.caption)
                        .bold()
                    TextEditor(text: .constant(latex))
                        .font(.system(.caption, design: .monospaced))
                        .textSelection(.enabled)
                        .frame(minHeight: 160)
                }
            }

            Spacer()

            HStack {
                Button("Close") {
                    dismiss()
                }
                .keyboardShortcut(.cancelAction)
                
                Button("Open Job URL") { state.openURL(item.jobURL) }
                if !retryLockedInCurrentMode && !(retryMode == .realSubmit && item.isApplied) {
                    Button(retryMode.applyButtonLabel) {
                        Task { await state.retry(item, mode: retryMode.retryModeKey) }
                    }
                }
                Spacer()
                Button("Delete from History", role: .destructive) {
                    showDeleteConfirmation = true
                }
                .confirmationDialog(
                    "Delete this application from history?",
                    isPresented: $showDeleteConfirmation,
                    titleVisibility: .visible
                ) {
                    Button("Delete from History", role: .destructive) {
                        Task {
                            await state.deleteApplication(item)
                            dismiss()
                        }
                    }
                    Button("Cancel", role: .cancel) {}
                } message: {
                    Text("This removes the row from history and deletes its stored application record.")
                }
            }
        }
        .padding()
        .frame(width: 620, height: 520)
    }
}

struct HistoryWindow: View {
    @EnvironmentObject private var state: AppState
    @Environment(\.dismiss) private var dismiss
    @State private var query = ""
    @State private var showClearHistoryConfirmation = false
    /// Which mode the History window is currently showing. Mirrors the
    /// segmented picker in the Console window — exactly the same control,
    /// flipping between two filtered views of the same dataset.
    @State private var selectedMode: HistorySectionMode = .dryRun

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("History")
                    .font(.headline)
                Spacer()
                Button("Refresh") {
                    Task { await state.refresh() }
                }
                Button("Clear History", role: .destructive) {
                    showClearHistoryConfirmation = true
                }
                .confirmationDialog(
                    "Clear all JobPilot history?",
                    isPresented: $showClearHistoryConfirmation,
                    titleVisibility: .visible
                ) {
                    Button("Clear History", role: .destructive) {
                        Task { await state.clearHistory() }
                    }
                    Button("Cancel", role: .cancel) {}
                } message: {
                    Text("This removes applications, runs, events, and pending actions. Profile data and templates are not deleted.")
                }
                Button("Close") {
                    dismiss()
                }
                .keyboardShortcut(.cancelAction)
            }

            HStack {
                TextField("Search company, role, URL, or location", text: $query)
                    .textFieldStyle(.roundedBorder)
            }

            // Segmented picker: same shape as the Console's Backend / API tabs.
            // Switching tabs filters the data set; rows are coloured by the
            // per-mode outcome so the user sees what's red and what's green
            // exclusively for that mode.
            Picker("", selection: $selectedMode) {
                Text("Dry Runs").tag(HistorySectionMode.dryRun)
                Text("Real Submits").tag(HistorySectionMode.realSubmit)
            }
            .pickerStyle(.segmented)

            HStack(spacing: 8) {
                StatusPill(
                    text: "\(activeSection.count) Roles",
                    color: .blue
                )
                StatusPill(
                    text: "\(greenCount) Green",
                    color: .green
                )
                StatusPill(
                    text: "\(redCount) Red",
                    color: .red
                )
                StatusPill(
                    text: "\(groupedByCompany.count) Companies",
                    color: .gray
                )
                Spacer()
            }

            // Company-grouped list. Tap a company row to expand its open jobs;
            // mirrors the original "click company → see roles" affordance the
            // user remembered from before, but scoped to the current mode tab.
            List {
                if groupedByCompany.isEmpty {
                    Text(emptyStateMessage)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else {
                    ForEach(groupedByCompany, id: \.company) { group in
                        DisclosureGroup {
                            ForEach(group.items) { item in
                                HistoryRoleRow(item: item, mode: selectedMode)
                                    .environmentObject(state)
                            }
                        } label: {
                            HStack(spacing: 8) {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(group.company)
                                        .font(.caption)
                                        .bold()
                                    Text("\(group.items.count) role\(group.items.count == 1 ? "" : "s")")
                                        .font(.caption2)
                                        .foregroundStyle(.secondary)
                                }
                                Spacer()
                                if group.greenCount > 0 {
                                    StatusPill(text: "\(group.greenCount) Green", color: .green)
                                }
                                if group.redCount > 0 {
                                    StatusPill(text: "\(group.redCount) Red", color: .red)
                                }
                            }
                        }
                    }
                }
            }
        }
        .padding()
        .frame(width: 760, height: 620)
        .toolbar {
            ToolbarItem(placement: .cancellationAction) {
                Button("Close") { dismiss() }
            }
        }
    }

    private var filteredApplications: [ApplicationRow] {
        state.allApplications.filter { item in
            let q = query.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
            if q.isEmpty { return true }
            return [
                item.displayCompany,
                item.title ?? "",
                item.jobURL,
                item.location ?? "",
                item.applicationType ?? ""
            ].map { $0.lowercased() }.contains { $0.contains(q) }
        }
    }

    /// The list of rows to show under the currently selected segment.
    /// Legacy rows (no per-mode outcome yet) default into the Dry Runs tab so
    /// existing data is still visible after the schema update.
    private var activeSection: [ApplicationRow] {
        let items: [ApplicationRow]
        switch selectedMode {
        case .dryRun:
            items = filteredApplications.filter {
                $0.hasDryRunAttempt || (!$0.hasRealSubmitAttempt && !$0.isApplied)
            }
        case .realSubmit:
            items = filteredApplications.filter {
                $0.hasRealSubmitAttempt || $0.isApplied
            }
        }
        return items.sorted { historySortKey($0) > historySortKey($1) }
    }

    private func sectionGreen(_ item: ApplicationRow) -> Bool {
        switch selectedMode {
        case .dryRun: return item.dryRunSucceeded
        case .realSubmit: return item.realSubmitSucceeded
        }
    }

    private var greenCount: Int { activeSection.filter { sectionGreen($0) }.count }
    private var redCount: Int { activeSection.filter { !sectionGreen($0) }.count }

    private var emptyStateMessage: String {
        let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines)
        switch selectedMode {
        case .dryRun:
            return trimmed.isEmpty ? "No dry-run attempts yet." : "No matching dry-run attempts."
        case .realSubmit:
            return trimmed.isEmpty ? "No real submissions yet." : "No matching real submissions."
        }
    }

    /// Roles grouped under each company (most-recent companies first). Each
    /// company row is a SwiftUI DisclosureGroup so users can drill in.
    private var groupedByCompany: [(company: String, items: [ApplicationRow], greenCount: Int, redCount: Int)] {
        let groups = Dictionary(grouping: activeSection) { $0.displayCompany }
        return groups.keys.sorted { lhs, rhs in
            let lhsLatest = groups[lhs]?.map { historySortKey($0) }.max() ?? ""
            let rhsLatest = groups[rhs]?.map { historySortKey($0) }.max() ?? ""
            if lhsLatest == rhsLatest { return lhs < rhs }
            return lhsLatest > rhsLatest
        }.map { company in
            let items = (groups[company] ?? []).sorted {
                let lhs = historySortKey($0)
                let rhs = historySortKey($1)
                if lhs == rhs { return ($0.title ?? "") < ($1.title ?? "") }
                return lhs > rhs
            }
            return (
                company: company,
                items: items,
                greenCount: items.filter { sectionGreen($0) }.count,
                redCount: items.filter { !sectionGreen($0) }.count
            )
        }
    }

    private func historySortKey(_ item: ApplicationRow) -> String {
        "\(item.appliedAt ?? "") \(item.title ?? "") \(item.jobURL)"
    }
}

/// Distinguishes which section of the History a row is being rendered in. The
/// row uses this to colour the dot, pick the per-mode status text, and decide
/// whether the Apply button should be enabled.
enum HistorySectionMode {
    case dryRun
    case realSubmit

    var retryModeKey: String {
        switch self {
        case .dryRun: return "dry_run"
        case .realSubmit: return "real_submit"
        }
    }

    var applyButtonLabel: String {
        switch self {
        case .dryRun: return "Dry Run Again"
        case .realSubmit: return "Real Submit Again"
        }
    }
}

private struct HistoryRoleRow: View {
    @EnvironmentObject private var state: AppState
    let item: ApplicationRow
    let mode: HistorySectionMode

    private var isGreen: Bool {
        switch mode {
        case .dryRun: return item.dryRunSucceeded
        case .realSubmit: return item.realSubmitSucceeded
        }
    }

    private var statusText: String {
        switch mode {
        case .dryRun:
            if item.dryRunSucceeded { return "Dry run passed" }
            if let err = item.dryRunError, !err.isEmpty {
                return "Dry run failed: \(err)"
            }
            if let outcome = item.dryRunOutcome, !outcome.isEmpty {
                return "Dry run: \(outcome)"
            }
            return item.displayStatus
        case .realSubmit:
            if item.realSubmitSucceeded { return "Submitted" }
            if let err = item.realSubmitError, !err.isEmpty {
                return "Submit failed: \(err)"
            }
            if let outcome = item.realSubmitOutcome, !outcome.isEmpty {
                return "Submit: \(outcome)"
            }
            return item.displayStatus
        }
    }

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Circle()
                .fill(isGreen ? Color.green : Color.red)
                .frame(width: 8, height: 8)
                .padding(.top, 5)
            VStack(alignment: .leading, spacing: 3) {
                Text(item.title ?? "Untitled role")
                    .font(.caption)
                    .bold()
                Text([item.displayCompany, item.location, item.applicationType].compactMap { $0 }.joined(separator: " | "))
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                Text(item.jobURL)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                Text(statusText)
                    .font(.caption2)
                    .foregroundStyle(isGreen ? .green : .red)
                    .lineLimit(2)
            }
            Spacer()
            StatusPill(text: isGreen ? "Green" : "Red", color: isGreen ? .green : .red)
            Button("Details") { state.selectedApplication = item }
                .font(.caption)
            // Only show the Apply button for red rows in this section. A green
            // row in DRY RUN cannot be dry-run again; a green row in REAL
            // SUBMITS cannot be re-submitted.
            if !isGreen {
                Button(mode.applyButtonLabel) {
                    Task { await state.retry(item, mode: mode.retryModeKey) }
                }
                .font(.caption)
                .buttonStyle(.borderedProminent)
            }
        }
        .padding(.vertical, 4)
    }
}

struct ConsoleLogsWindow: View {
    @EnvironmentObject private var state: AppState
    @State private var selectedTab = "backend"

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Console Logs")
                        .font(.headline)
                    Text(activeLogsDir)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                }
                Spacer()
                Button("Refresh") {
                    Task { await state.refreshLogs() }
                }
                Button("Copy All") {
                    state.copyConsoleLogs()
                }
                Button("Clear Logs") {
                    Task { await state.clearLogs() }
                }
                Button("Open Logs Folder") {
                    state.openLogsFolder()
                }
            }

            if let error = state.consoleError {
                Label(error, systemImage: "xmark.octagon")
                    .font(.caption)
                    .foregroundStyle(.red)
            }

            Picker("", selection: $selectedTab) {
                Text("Backend Console").tag("backend")
                Text("Backend Errors").tag("backend_stderr")
                Text("API Output").tag("stdout")
                Text("API Errors").tag("stderr")
                Text("All").tag("all")
            }
            .pickerStyle(.segmented)

            TextEditor(text: .constant(selectedText))
                .font(.system(.caption, design: .monospaced))
                .textSelection(.enabled)
                .overlay(
                    RoundedRectangle(cornerRadius: 8)
                        .stroke(Color.gray.opacity(0.18))
                )

            HStack {
                Text("\(selectedText.count) characters shown")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                Spacer()
                Button("Close") {
                    state.showConsole = false
                }
            }
        }
        .padding()
        .frame(width: 920, height: 680)
        .task {
            await state.refreshLogs()
        }
    }

    private var activeLogsDir: String {
        if selectedTab.hasPrefix("backend") {
            return state.backendConsoleLogs.logsDir.isEmpty ? "App-launched backend console files" : state.backendConsoleLogs.logsDir
        }
        return state.consoleLogs.logsDir.isEmpty ? "Backend API log files" : state.consoleLogs.logsDir
    }

    private var selectedText: String {
        switch selectedTab {
        case "backend":
            return state.backendConsoleLogs.combinedText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                ? "No backend console output yet."
                : state.backendConsoleLogs.combinedText
        case "backend_stderr":
            return state.backendConsoleLogs.stderr.isEmpty ? "No backend stderr logs yet." : state.backendConsoleLogs.stderr
        case "stdout":
            return state.consoleLogs.stdout.isEmpty ? "No stdout logs yet." : state.consoleLogs.stdout
        case "all":
            return """
            ===== APP-LAUNCHED BACKEND CONSOLE =====
            \(state.backendConsoleLogs.combinedText)

            ===== BACKEND API LOGS =====
            \(state.consoleLogs.combinedText)
            """
        default:
            return state.consoleLogs.stderr.isEmpty ? "No stderr logs yet." : state.consoleLogs.stderr
        }
    }
}

private struct DetailLine: View {
    let label: String
    let value: String

    var body: some View {
        GridRow {
            Text(label)
                .font(.caption)
                .foregroundStyle(.secondary)
            Text(value)
                .font(.caption)
                .textSelection(.enabled)
        }
    }
}

private struct FileActionRow: View {
    @EnvironmentObject private var state: AppState
    let label: String
    let path: String

    var body: some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text(label)
                    .font(.caption)
                    .bold()
                Text(path)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .textSelection(.enabled)
            }
            Spacer()
            Button("Open") { state.openPath(path) }
        }
    }
}
