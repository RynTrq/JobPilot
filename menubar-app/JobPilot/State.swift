import AppKit
import Combine
import Darwin
import Foundation

@MainActor
final class AppState: ObservableObject {
    @Published var careerURL: String = ""
    @Published var dailyLimitEnabled: Bool = false
    @Published var dailyLimit: Int = 25
    @Published var status: BackendStatus = BackendStatus()
    @Published var isRunning: Bool = false
    @Published var todayCount: Int = 0
    @Published var alarmPending: Bool = false
    @Published var alarmField: String = ""
    @Published var alarm: PendingAlarm?
    @Published var showAlarmWindow: Bool = false
    @Published var alarmAnswer: String = ""
    @Published var classifierReview: PendingClassifierReview?
    @Published var showClassifierReview: Bool = false
    @Published var manualTakeover: PendingManualTakeover?
    @Published var showManualTakeover: Bool = false
    @Published var approval: PendingApproval?
    @Published var showApproval: Bool = false
    @Published var activity: [ApplicationRow] = []
    @Published var allApplications: [ApplicationRow] = []
    @Published var runs: [RunRow] = []
    @Published var settings: BackendSettings = BackendSettings()
    @Published var lastEventText: String = "Idle"
    @Published var events: [TimelineEvent] = []
    @Published var selectedApplication: ApplicationRow?
    @Published var showConsole: Bool = false
    @Published var showAlarmSettings: Bool = false
    @Published var consoleLogs: BackendLogs = BackendLogs()
    @Published var backendConsoleLogs: BackendConsoleLogs = BackendConsoleLogs()
    @Published var consoleError: String?
    @Published var backendPort: Int = BackendClient.defaultPort
    /// Set when an SSE error event with error_code "adapter_not_found" fires.
    /// Cleared on each new run start. Drives the Unknown Adapter banner in MenuBarView.
    @Published var unknownAdapterSite: String? = nil
    /// The escalating multi-channel alarm. Owns sound, speech, notifications, dock bounce.
    /// State is forwarded through `alarmIsRinging` / `alarmLevel` for SwiftUI binding.
    let alarmManager = AlarmManager()
    @Published var alarmIsRinging: Bool = false
    @Published var alarmLevelLabel: String = "Idle"
    @Published var alarmIsSnoozed: Bool = false
    /// True when the backend's /health endpoint returned 2xx within the last ping.
    /// Drives the "Backend not reachable" banner so users never stare at stale state
    /// wondering why nothing's happening. Updated every `healthPollSeconds`.
    @Published var backendReachable: Bool = false
    private var streamTask: Task<Void, Never>?
    private var healthTask: Task<Void, Never>?
    private var dashboardTask: Task<Void, Never>?
    private var backendBootstrapTask: Task<Void, Never>?
    private let healthPollSeconds: UInt64 = 3
    private var backendProcess: Process?
    private var backendStartInFlight = false
    private var lastBackendStartAttempt = Date.distantPast
    private var lastDashboardRefresh = Date.distantPast
    private var isRefreshingDashboard = false
    private var alarmCancellable: AnyCancellable?

    let backend = BackendClient()

    var backendEndpoint: String {
        "127.0.0.1:\(backendPort)"
    }

    private var runLimit: Int? {
        dailyLimitEnabled ? max(1, dailyLimit) : nil
    }

    init() {
        // Sync the alarm-manager's state into our @Published fields whenever it changes,
        // so any SwiftUI view bound to AppState reacts to alarm transitions immediately.
        alarmCancellable = alarmManager.objectWillChange
            .receive(on: RunLoop.main)
            .sink { [weak self] _ in
                self?.syncFromAlarmManager()
            }
        syncFromAlarmManager()
        // Clean startup. As soon as the @StateObject lives (which happens
        // very early during App body evaluation, before any window appears),
        // we kick off the backend bootstrap. This guarantees the backend is
        // started and confirmed reachable BEFORE the user can interact with
        // the menubar dropdown or open any window — the order the user asked
        // for: backend first, then frontend.
        Task { [weak self] in
            await self?.bootstrap()
        }
    }

    deinit {
        streamTask?.cancel()
        healthTask?.cancel()
        dashboardTask?.cancel()
        backendBootstrapTask?.cancel()
        alarmCancellable?.cancel()
    }

    /// Kick off a cheap background health-polling loop so the UI always knows whether
    /// the backend is actually up. Safe to call repeatedly (guarded by `healthTask`).
    func startHealthMonitor() {
        guard healthTask == nil else { return }
        startLocalBackendIfNeeded()
        healthTask = Task { [weak self] in
            guard let self else { return }
            while !Task.isCancelled {
                let ok = await self.backend.isReachable()
                await MainActor.run { [weak self] in
                    guard let self else { return }
                    if self.backendReachable != ok {
                        self.backendReachable = ok
                        if !ok {
                            self.recordEvent(title: "Backend unreachable", detail: "No response on \(self.backendEndpoint). Is the server running?", level: .warning)
                            self.startLocalBackendIfNeeded()
                        } else {
                            self.recordEvent(title: "Backend reachable", detail: "Health check passed.", level: .info)
                        }
                    }
                }
                try? await Task.sleep(nanoseconds: self.healthPollSeconds * 1_000_000_000)
            }
        }
    }

    func startDashboardMonitor() {
        guard dashboardTask == nil else { return }
        dashboardTask = Task { [weak self] in
            guard let self else { return }
            while !Task.isCancelled {
                let shouldPoll = await MainActor.run { [weak self] in
                    guard let self else { return false }
                    return self.backendReachable && (self.isRunning || self.alarm != nil || self.approval != nil || self.classifierReview != nil || self.manualTakeover != nil)
                }
                if shouldPoll {
                    await self.scheduleDashboardRefresh(force: false)
                }
                try? await Task.sleep(nanoseconds: 2_000_000_000)
            }
        }
    }

    /// Pull the current alarm-manager snapshot into our @Published mirrors. AlarmManager
    /// is @MainActor and synchronously readable from here.
    private func syncFromAlarmManager() {
        alarmIsRinging = alarmManager.isRinging
        alarmLevelLabel = alarmManager.isRinging ? alarmManager.level.label : "Idle"
        alarmIsSnoozed = alarmManager.isSnoozed
    }

    func refresh() async {
        do {
            try await loadDashboard()
        } catch {
            status.state = "starting backend"
            recordEvent(title: "Backend starting", detail: "Waiting for the local backend to finish startup.", level: .info)
            do {
                try await waitForBackend()
                try await loadDashboard()
            } catch {
                status.state = "offline: \(shortError(error))"
                recordEvent(title: "Backend start failed", detail: shortError(error), level: .error)
            }
        }
    }

    func start() async {
        guard !careerURL.isEmpty else { return }
        normalizeDailyLimit()
        unknownAdapterSite = nil
        do {
            try await waitForBackend()
            _ = try await backend.start(careerURL: careerURL, limit: runLimit)
            recordEvent(title: "Run started", detail: careerURL, level: .info)
            await scheduleDashboardRefresh(force: true)
        } catch {
            status.state = "start failed: \(shortError(error))"
            recordEvent(title: "Start failed", detail: shortError(error), level: .error)
        }
    }

    func normalizeDailyLimit() {
        dailyLimit = max(1, dailyLimit)
    }

    func stop() async {
        try? await backend.stop()
        recordEvent(title: "Stop requested", detail: "The current run will stop after the active job finishes.", level: .warning)
        await scheduleDashboardRefresh(force: true)
    }

    func retry(_ item: ApplicationRow) async {
        await retry(item, mode: nil)
    }

    /// Re-attempt a job in either dry-run or real-submit mode. If `mode` is supplied
    /// we flip the live-submit toggle to match before kicking off the run, so the
    /// History UI can offer two distinct retry buttons (one per section).
    func retry(_ item: ApplicationRow, mode: String?) async {
        normalizeDailyLimit()
        if let mode {
            let wantLive = (mode == "real_submit")
            if settings.liveSubmitEnabled != wantLive {
                await setLiveSubmit(wantLive)
            }
        }
        do {
            try await waitForBackend()
            _ = try await backend.start(
                careerURL: item.jobURL,
                limit: 1,
                forceReprocess: true,
                bypassClassifier: true
            )
            let label: String
            switch mode {
            case "dry_run": label = "Dry-run retry started"
            case "real_submit": label = "Real-submit retry started"
            default: label = "Apply override started"
            }
            recordEvent(title: label, detail: item.title ?? item.jobURL, level: .info)
            await scheduleDashboardRefresh(force: true)
        } catch {
            status.state = "start failed: \(shortError(error))"
            recordEvent(title: "Apply override failed", detail: shortError(error), level: .error)
        }
    }

    func deleteApplication(_ item: ApplicationRow) async {
        do {
            try await backend.deleteApplication(jobURL: item.jobURL)
            activity.removeAll { $0.jobURL == item.jobURL }
            allApplications.removeAll { $0.jobURL == item.jobURL }
            if selectedApplication?.jobURL == item.jobURL {
                selectedApplication = nil
            }
            recordEvent(title: "Removed from history", detail: item.title ?? item.jobURL, level: .info)
            await scheduleDashboardRefresh(force: true)
        } catch {
            recordEvent(title: "Could not remove from history", detail: shortError(error), level: .error)
        }
    }

    func deleteApplicationsByCompany(_ companies: Set<String>) async {
        guard !companies.isEmpty else { return }
        let list = Array(companies)
        do {
            try await backend.deleteApplicationsByCompany(companies: list)
            allApplications.removeAll { list.contains($0.displayCompany) }
            activity.removeAll { list.contains($0.company ?? "") }
            if let sel = selectedApplication, list.contains(sel.displayCompany) {
                selectedApplication = nil
            }
            recordEvent(title: "Removed \(list.count) company/companies from history", detail: list.joined(separator: ", "), level: .info)
            await scheduleDashboardRefresh(force: true)
        } catch {
            recordEvent(title: "Could not remove companies from history", detail: shortError(error), level: .error)
        }
    }

    func clearHistory() async {
        do {
            try await backend.clearHistory()
            activity.removeAll()
            allApplications.removeAll()
            runs.removeAll()
            selectedApplication = nil
            alarm = nil
            alarmPending = false
            approval = nil
            classifierReview = nil
            manualTakeover = nil
            recordEvent(title: "History cleared", detail: "Applications, runs, events, and pending actions were removed.", level: .info)
            await scheduleDashboardRefresh(force: true)
        } catch {
            recordEvent(title: "Could not clear history", detail: shortError(error), level: .error)
        }
    }

    func refreshLogs() async {
        refreshBackendConsoleLogs()
        do {
            consoleLogs = try await backend.logs()
            consoleError = nil
        } catch {
            consoleError = "Backend API logs unavailable: \(shortError(error))"
        }
    }

    func copyConsoleLogs() {
        let combined = """
        ===== APP-LAUNCHED BACKEND CONSOLE =====
        \(backendConsoleLogs.combinedText)

        ===== BACKEND API LOGS =====
        \(consoleLogs.combinedText)
        """
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(combined, forType: .string)
        recordEvent(title: "Console logs copied", detail: "\(combined.count) characters copied to clipboard.", level: .info)
    }

    func clearLogs() async {
        clearLocalBackendConsoleLogs()
        refreshBackendConsoleLogs()
        do {
            try await backend.clearLogs()
            consoleLogs = try await backend.logs()
            consoleError = nil
            recordEvent(title: "Console logs cleared", detail: "Old backend stdout and stderr log files were emptied.", level: .info)
        } catch {
            consoleError = "Backend API logs unavailable: \(shortError(error))"
            recordEvent(title: "Launch logs cleared", detail: "The local backend console files were cleared; API logs were unavailable.", level: .warning)
        }
    }

    func setLiveSubmit(_ enabled: Bool) async {
        do {
            settings = try await backend.setLiveSubmit(enabled: enabled)
            recordEvent(title: enabled ? "Real submit enabled" : "Dry run enabled", detail: enabled ? "Final approval can now submit real forms." : "Final submits are skipped.", level: enabled ? .warning : .info)
        } catch {
            recordEvent(title: "Could not update real submit", detail: shortError(error), level: .error)
        }
    }

    func setFinalReviewRequired(_ required: Bool) async {
        do {
            settings = try await backend.setAutoSubmit(enabled: !required)
            recordEvent(
                title: required ? "Final review required" : "Final review skipped",
                detail: required ? "Every job pauses for final approval before the submit/dry-run step." : "Clean jobs continue without the final approval window. Warnings, missing fields, login, captcha, or errors still pause.",
                level: required ? .info : .warning
            )
        } catch {
            recordEvent(title: "Could not update final review", detail: shortError(error), level: .error)
        }
    }

    func setClassifierAutoPass(_ enabled: Bool) async {
        do {
            settings = try await backend.setClassifierAutoPass(enabled: enabled)
            recordEvent(
                title: enabled ? "Auto-classify enabled" : "Classifier review enabled",
                detail: enabled
                    ? "Classifier scores at or above the threshold proceed without a review prompt."
                    : "Each classifier pass is confirmed before continuing, and the answer trains future runs.",
                level: enabled ? .warning : .info
            )
        } catch {
            recordEvent(title: "Could not update auto-classify", detail: shortError(error), level: .error)
        }
    }

    /// Toggle Watch Browser: visible automation browser, auto-bring-to-front on alarm, allows direct take-over.
    func setLiveMode(_ enabled: Bool) async {
        do {
            settings = try await backend.setLiveMode(enabled: enabled)
            recordEvent(
                title: enabled ? "Watch browser on" : "Watch browser off",
                detail: enabled
                    ? "The automation browser will be brought to the front during runs. When the app pauses, type directly into the form and press \"Use browser input\" on the alarm popup."
                    : "Form filling will continue in the background.",
                level: .info
            )
        } catch {
            recordEvent(title: "Could not update watch browser", detail: shortError(error), level: .error)
        }
    }

    /// Read whatever the user typed into the focused automation-browser field and use it as the alarm answer.
    /// The backend will record this so the app learns from the manual input.
    func useBrowserAnswer() async {
        guard let alarm else { return }
        do {
            let value = try await backend.readBrowserAnswer(question: alarm.question)
            if let value, !value.isEmpty {
                alarmAnswer = value
                try? await backend.fillGap(question: alarm.question, answer: value)
                recordEvent(title: "Used browser input", detail: "Read \"\(value)\" from the focused browser field.", level: .info)
                self.alarm = nil
                self.alarmAnswer = ""
                self.alarmPending = false
                self.alarmField = ""
                self.showAlarmWindow = false
                stopAlarm()
            } else {
                recordEvent(title: "No focused field in browser", detail: "Click into the field in the automation browser first, then press Use browser input.", level: .warning)
            }
        } catch {
            recordEvent(title: "Could not read from browser", detail: shortError(error), level: .error)
        }
    }

    func focusAutomationBrowser() async {
        do {
            let url = try await backend.focusBrowser()
            recordEvent(title: "Automation browser focused", detail: url ?? status.currentJob, level: .info)
        } catch {
            recordEvent(title: "Could not focus automation browser", detail: shortError(error), level: .error)
        }
    }

    func openCurrentPageInDefaultBrowser() async {
        do {
            let url = try await backend.openCurrentPageInDefaultBrowser()
            if let url {
                openURL(url)
            }
            recordEvent(title: "Opened current page in default browser", detail: url ?? status.currentJob, level: .info)
        } catch {
            recordEvent(title: "Could not open current page in default browser", detail: shortError(error), level: .error)
        }
    }

    func startStream() {
        guard streamTask == nil else { return }
        startLocalBackendIfNeeded()
        streamTask = Task { [weak self] in
            // Reconnect loop: if the SSE drops mid-run we don't want to require a manual
            // refresh. Capacity to backoff is in `delays` below.
            let delays: [UInt64] = [1, 2, 4, 8, 8] // seconds
            var attempt = 0
            while !Task.isCancelled {
                guard let self else { return }
                do {
                    try await self.waitForBackend()
                    attempt = 0
                    await self.scheduleDashboardRefresh(force: true)
                    let stream = await self.backend.streamEvents()
                    for try await event in stream {
                        if Task.isCancelled { return }
                        await self.handle(event)
                    }
                    // Stream ended cleanly — reconnect after the smallest delay.
                    self.status.state = "stream reconnecting"
                } catch {
                    if Task.isCancelled { return }
                    self.status.state = "stream offline"
                    self.recordEvent(title: "Live stream disconnected", detail: self.shortError(error), level: .warning)
                }
                let wait = delays[min(attempt, delays.count - 1)]
                attempt += 1
                try? await Task.sleep(nanoseconds: wait * 1_000_000_000)
            }
        }
    }

    /// Cancel the SSE reconnect loop. Useful for tests or for explicitly going offline.
    func stopStream() {
        streamTask?.cancel()
        streamTask = nil
    }

    func submitAlarm() async {
        guard let alarm else { return }
        try? await backend.fillGap(question: alarm.question, answer: alarmAnswer)
        recordEvent(title: "Answered missing field", detail: alarm.question, level: .info)
        self.alarm = nil
        self.alarmAnswer = ""
        self.alarmPending = false
        self.alarmField = ""
        self.showAlarmWindow = false
        stopAlarm()
    }

    func skipAlarmField() async {
        guard let alarm else { return }
        try? await backend.skipField(question: alarm.question)
        recordEvent(title: "Skipped field (always skip)", detail: alarm.question, level: .warning)
        self.alarm = nil
        self.alarmAnswer = ""
        self.alarmPending = false
        self.alarmField = ""
        self.showAlarmWindow = false
        stopAlarm()
    }

    func openAlarm() {
        guard alarm != nil else { return }
        showAlarmWindow = true
    }

    func respondToApproval(_ approved: Bool) async {
        guard let approval else { return }
        try? await backend.respondToApproval(token: approval.token, approved: approved, fieldAnswers: approval.fieldAnswers)
        recordEvent(title: approved ? "Approved final step" : "Skipped final step", detail: approval.title ?? approval.jobURL ?? "", level: approved ? .success : .warning)
        self.approval = nil
        self.showApproval = false
        stopAlarm()
    }

    func respondToClassifierReview(_ passed: Bool) async {
        guard let classifierReview else { return }
        try? await backend.respondToClassifierReview(token: classifierReview.token, passed: passed)
        self.classifierReview = nil
        self.showClassifierReview = false
        lastEventText = passed ? "Classifier review: passed" : "Classifier review: failed"
        recordEvent(title: lastEventText, detail: classifierReview.title ?? classifierReview.jobURL ?? "", level: passed ? .success : .warning)
        stopAlarm()
    }

    func openClassifierReview() {
        guard classifierReview != nil else { return }
        showClassifierReview = true
    }

    func openApproval() {
        guard approval != nil else { return }
        showApproval = true
    }

    func respondToManualTakeover(_ action: String, buttonType: String? = nil, buttonText: String? = nil) async {
        guard let manualTakeover else { return }
        try? await backend.respondToManualTakeover(token: manualTakeover.token, action: action, buttonType: buttonType, buttonText: buttonText)
        recordEvent(title: action == "continue" ? "Manual takeover complete" : "Manual takeover skipped", detail: manualTakeover.title ?? manualTakeover.jobURL, level: action == "continue" ? .success : .warning)
        self.manualTakeover = nil
        self.showManualTakeover = false
        stopAlarm()
    }

    func openManualTakeover() {
        guard manualTakeover != nil else { return }
        showManualTakeover = true
    }

    func updateApprovalField(_ field: ReviewField, value: String) {
        guard let approval else { return }
        var copy = approval
        if let index = copy.fieldAnswers.firstIndex(where: { $0.id == field.id }) {
            copy.fieldAnswers[index].value = value
            self.approval = copy
        }
    }

    private func handle(_ event: SSEEvent) async {
        record(event)
        if event.name == "classifier_review_required" {
            lastEventText = "Classifier review needed"
            let title = event.data["title"] as? String
            classifierReview = PendingClassifierReview(
                token: event.data["token"] as? String ?? "",
                company: event.data["company"] as? String,
                title: title,
                jobURL: event.data["job_url"] as? String,
                classifierScore: event.data["classifier_score"] as? Double,
                location: event.data["location"] as? String,
                descriptionPreview: event.data["description_preview"] as? String,
                descriptionText: event.data["description_text"] as? String,
                fitDecisionSummary: event.data["fit_decision_summary"] as? String
            )
            showClassifierReview = true
            startAlarm(kind: .classifierReview(title: title))
        } else if event.name == "manual_takeover_required" {
            lastEventText = "Manual browser takeover needed"
            let title = event.data["title"] as? String
            manualTakeover = PendingManualTakeover(
                token: event.data["token"] as? String ?? "",
                company: event.data["company"] as? String,
                title: title,
                jobURL: event.data["job_url"] as? String,
                reason: event.data["reason"] as? String,
                currentURL: event.data["current_url"] as? String,
                allowButtonNameRegistration: event.data["allow_button_name_registration"] as? Bool
            )
            showManualTakeover = true
            startAlarm(kind: .manualTakeover(title: title))
        } else if event.name == "alarm" {
            let question = event.data["question"] as? String ?? ""
            let fieldLabel = event.data["field_label"] as? String ?? question
            let fieldType = event.data["field_type"] as? String ?? "text"
            let options = event.data["options"] as? [String] ?? []
            let token = event.data["token"] as? String ?? ""
            lastEventText = "Needs your answer: \(question)"
            alarm = PendingAlarm(token: token, question: question, fieldType: fieldType, options: options)
            alarmPending = true
            alarmField = fieldLabel
            showAlarmWindow = true
            startAlarm(kind: .missingAnswer(question: question))
        } else if event.name == "approval_required" {
            let rawFields = event.data["field_answers"] as? [[String: Any]] ?? []
            let title = event.data["title"] as? String
            lastEventText = "Final review ready"
            approval = PendingApproval(
                token: event.data["token"] as? String ?? "",
                company: event.data["company"] as? String,
                title: title,
                jobURL: event.data["job_url"] as? String,
                classifierScore: event.data["classifier_score"] as? Double,
                descriptionText: event.data["description_text"] as? String,
                resumePath: event.data["resume_path"] as? String,
                coverLetterPath: event.data["cover_letter_path"] as? String,
                fieldAnswers: rawFields.map(ReviewField.init(payload:)),
                validationWarnings: (event.data["validation_warnings"] as? [[String: Any]] ?? []).map(ValidationWarning.init(payload:)),
                missingRequiredFields: event.data["missing_required_fields"] as? [String] ?? [],
                dryRun: event.data["dry_run"] as? Bool ?? true
            )
            showApproval = true
            startAlarm(kind: .finalApproval(title: title))
        } else if event.name == "progress" {
            let type = event.data["type"] as? String ?? "progress"
            let message = event.data["message"] as? String
            lastEventText = message ?? type.replacingOccurrences(of: "_", with: " ")
            if type == "stage" {
                // Update current stage directly from the stream so the UI feels live between /status polls.
                if let stage = event.data["stage"] as? String {
                    status.currentStage = stage
                }
                if let currentJob = event.data["current_job"] as? String, !currentJob.isEmpty {
                    status.currentJob = currentJob
                }
                status.currentStageMessage = message
                if let stateValue = event.data["state"] as? String, !stateValue.isEmpty {
                    status.state = stateValue
                    isRunning = ["running", "stopping", "starting"].contains(stateValue)
                }
            }
            await scheduleDashboardRefresh(force: type == "dry_run_complete" || type == "needs_attention" || type == "limit_hit")
        } else if event.name == "applied" || event.name == "error" {
            lastEventText = event.name
            if event.name == "error", event.data["error_code"] as? String == "adapter_not_found" {
                unknownAdapterSite = careerURL.trimmingCharacters(in: .whitespacesAndNewlines)
            }
            await scheduleDashboardRefresh(force: true)
        }
    }

    func clearEvents() {
        events = []
    }

    func openOutputsFolder() {
        openPath(defaultBackendDir() + "/data/outputs")
    }

    func openLogsFolder() {
        openPath(defaultBackendDir() + "/data/logs")
    }

    private func refreshBackendConsoleLogs(maxChars: Int = 120_000) {
        let logsDir = localBackendLogsDir()
        backendConsoleLogs = BackendConsoleLogs(
            logsDir: logsDir,
            stdout: readTail(path: "\(logsDir)/menubar-backend.stdout.log", maxBytes: maxChars),
            stderr: readTail(path: "\(logsDir)/menubar-backend.stderr.log", maxBytes: maxChars)
        )
    }

    private func clearLocalBackendConsoleLogs() {
        let logsDir = localBackendLogsDir()
        try? FileManager.default.createDirectory(atPath: logsDir, withIntermediateDirectories: true)
        for filename in ["menubar-backend.stdout.log", "menubar-backend.stderr.log"] {
            let url = URL(fileURLWithPath: logsDir).appendingPathComponent(filename)
            try? Data().write(to: url, options: .atomic)
        }
    }

    private func localBackendLogsDir() -> String {
        "\(defaultBackendDir())/data/logs"
    }

    private func readTail(path: String, maxBytes: Int) -> String {
        guard let data = FileManager.default.contents(atPath: path), !data.isEmpty else {
            return ""
        }
        let slice = data.count > maxBytes ? Data(data.suffix(maxBytes)) : data
        return String(decoding: slice, as: UTF8.self)
    }

    func openBackendFolder() {
        openPath(defaultBackendDir())
    }

    func openPath(_ path: String?) {
        guard let path, !path.isEmpty else { return }
        NSWorkspace.shared.open(URL(fileURLWithPath: path))
    }

    func openURL(_ string: String?) {
        guard let string, let url = URL(string: string) else { return }
        NSWorkspace.shared.open(url)
    }

    func openInChrome(_ string: String?) {
        guard let string, let url = URL(string: string) else { return }
        let chrome = URL(fileURLWithPath: "/Applications/Google Chrome.app")
        if FileManager.default.fileExists(atPath: chrome.path) {
            NSWorkspace.shared.open([url], withApplicationAt: chrome, configuration: NSWorkspace.OpenConfiguration())
        } else {
            NSWorkspace.shared.open(url)
        }
    }

    func startAlarm(kind: AlarmKind) {
        alarmManager.start(kind: kind)
        syncFromAlarmManager()
    }

    /// Backwards-compatible wrapper — starts a generic alarm when the kind isn't known.
    func startAlarm() {
        alarmManager.start(kind: .missingAnswer(question: alarm?.question ?? ""))
        syncFromAlarmManager()
    }

    func stopAlarm() {
        alarmManager.stop()
        syncFromAlarmManager()
    }

    func snoozeAlarm() {
        alarmManager.snooze()
        syncFromAlarmManager()
    }

    func escalateAlarmNow() {
        alarmManager.escalateNow()
        syncFromAlarmManager()
    }

    private func record(_ event: SSEEvent) {
        var title: String
        var detail: String?
        var level: TimelineLevel = .info
        switch event.name {
        case "classifier_review_required":
            title = "Classifier review needed"
            detail = event.data["title"] as? String ?? event.data["job_url"] as? String
            level = .warning
        case "approval_required":
            title = "Final approval needed"
            detail = event.data["title"] as? String ?? event.data["job_url"] as? String
            level = .warning
        case "manual_takeover_required":
            title = "Manual browser takeover needed"
            detail = event.data["reason"] as? String ?? event.data["job_url"] as? String
            level = .warning
        case "alarm":
            title = "Missing field answer"
            detail = event.data["question"] as? String
            level = .warning
        case "progress":
            let type = event.data["type"] as? String ?? "progress"
            title = type.replacingOccurrences(of: "_", with: " ").capitalized
            detail = event.data["message"] as? String ?? event.data["url"] as? String
            if type == "needs_attention" || type == "manual_review_required" {
                level = .error
                detail = event.data["error"] as? String ?? detail
            } else if type == "dry_run_complete" {
                level = .success
            } else if type == "empty_listings" {
                // The scraper found zero job links on the URL the user pasted. This used to
                // look identical to a working run and caused hours of confusion; now we make
                // it noisy with a warning-level event and an obvious hint in the detail line.
                level = .error
                title = "No jobs found on that URL"
                detail = event.data["hint"] as? String
                    ?? "The URL didn't return any job listings. Make sure it's a careers page, not a single job posting."
            }
        case "error":
            if event.data["error_code"] as? String == "adapter_not_found" {
                title = "No adapter found"
                detail = event.data["message"] as? String ?? event.data["recovery_hint"] as? String
            } else {
                title = "Backend error"
                detail = event.data["message"] as? String
            }
            level = .error
        default:
            title = event.name
            detail = event.data["message"] as? String ?? event.data["url"] as? String
        }
        recordEvent(title: title, detail: detail, level: level)
    }

    private func recordEvent(title: String, detail: String?, level: TimelineLevel) {
        let item = TimelineEvent(title: title, detail: detail, level: level, date: Date())
        events.insert(item, at: 0)
        if events.count > 80 {
            events.removeLast(events.count - 80)
        }
    }

    private func loadDashboard() async throws {
        if isRefreshingDashboard {
            return
        }
        isRefreshingDashboard = true
        defer {
            isRefreshingDashboard = false
            lastDashboardRefresh = Date()
        }
        async let latestStatus = backend.status()
        async let latestSettings = backend.settings()
        async let latestApplications = backend.applications(limit: 100)
        async let latestRuns = backend.runs(limit: 50)
        async let latestPendingActions = backend.pendingActions()
        let latest = try await latestStatus
        status = latest
        // "running" and "stopping" both keep the Stop button visible; "starting" shows the spinner too.
        isRunning = ["running", "stopping", "starting"].contains(latest.state)
        todayCount = latest.today
        activity = latest.last10
        settings = (try? await latestSettings) ?? settings
        allApplications = (try? await latestApplications) ?? allApplications
        runs = (try? await latestRuns) ?? runs
        if let pendingActions = try? await latestPendingActions {
            let previousApprovalToken = approval?.token
            classifierReview = pendingActions.classifierReviews.first
            approval = pendingActions.approvals.first
            manualTakeover = pendingActions.manualTakeovers.first
            if let approval {
                if previousApprovalToken != approval.token {
                    showApproval = true
                    startAlarm(kind: .finalApproval(title: approval.title))
                }
            } else if previousApprovalToken != nil && alarm == nil && classifierReview == nil && manualTakeover == nil {
                showApproval = false
                stopAlarm()
            }
            if let firstAlarm = pendingActions.alarms?.first {
                if self.alarm == nil || self.alarm?.token != firstAlarm.token {
                    self.alarm = PendingAlarm(token: firstAlarm.token, question: firstAlarm.question, fieldType: firstAlarm.fieldType ?? "text", options: firstAlarm.options ?? [])
                    self.alarmField = firstAlarm.question
                    self.alarmPending = true
                    self.showAlarmWindow = true
                }
            } else {
                if self.alarmPending {
                    self.alarm = nil
                    self.alarmPending = false
                    self.showAlarmWindow = false
                }
            }
        }
    }

    private func scheduleDashboardRefresh(force: Bool) async {
        if !force && Date().timeIntervalSince(lastDashboardRefresh) < 0.75 {
            return
        }
        do {
            try await loadDashboard()
        } catch {
            if force {
                recordEvent(title: "Dashboard refresh failed", detail: shortError(error), level: .warning)
            }
        }
    }

    /// Cleanly shut everything down before quitting the app. Called by the
    /// "Quit JobPilot" menu item AND by AppDelegate's
    /// applicationShouldTerminate (so Cmd-Q / Force Quit / Login-out also
    /// behave). Steps:
    ///  1. Ask the backend to stop any in-flight automation run so the browser
    ///     gets unwound and answers/artifacts get persisted.
    ///  2. Cancel every long-running Task (health, stream, dashboard, etc.) so
    ///     they can't fight us during shutdown.
    ///  3. SIGTERM the launched backend Process and any other listener still
    ///     holding the port; escalate to SIGKILL after a short grace period.
    ///
    /// If `replyToTerminate` is true, we tell AppKit to finish the deferred
    /// termination it started; otherwise (called from a button) we kick off a
    /// fresh `NSApp.terminate(nil)`.
    func shutdownAndQuit(replyToTerminate: Bool = false) async {
        recordEvent(title: "Quit requested", detail: "Stopping backend and child processes…", level: .info)

        if isRunning || backendReachable {
            do { try await backend.stop() } catch { /* ignore */ }
        }

        streamTask?.cancel(); streamTask = nil
        healthTask?.cancel(); healthTask = nil
        dashboardTask?.cancel(); dashboardTask = nil
        backendBootstrapTask?.cancel(); backendBootstrapTask = nil
        alarmCancellable?.cancel()
        alarmManager.stop()

        let port = backendPort
        if let process = backendProcess, process.isRunning {
            let pid = process.processIdentifier
            _ = kill(pid, SIGTERM)
            for _ in 0..<25 {
                if !process.isRunning { break }
                try? await Task.sleep(nanoseconds: 200_000_000)
            }
            if process.isRunning {
                _ = kill(pid, SIGKILL)
                process.waitUntilExit()
            }
            backendProcess = nil
        }
        let listeners = processIDsListening(on: port)
        if !listeners.isEmpty {
            await terminateListeners(listeners, on: port)
        }

        recordEvent(title: "Backend stopped", detail: "Goodbye.", level: .success)

        if replyToTerminate {
            NSApp.reply(toApplicationShouldTerminate: true)
        } else {
            NSApp.terminate(nil)
        }
    }

    /// Public bootstrap. Called once on launch. Starts the backend cleanly
    /// before any other monitor task connects, so the user never sees the
    /// menubar window flailing against a not-yet-running server.
    func bootstrap() async {
        // Wait for /health to come up. waitForBackend already handles "is it
        // up? if not, launch it and poll" and returns when it's reachable.
        do {
            try await waitForBackend()
            backendReachable = true
        } catch {
            backendReachable = false
            recordEvent(title: "Backend not ready", detail: shortError(error), level: .error)
        }
        // Now that the backend is up (or we've at least tried), start the
        // monitors and refresh dashboard data.
        startHealthMonitor()
        startDashboardMonitor()
        startStream()
        await refresh()
    }

    private func waitForBackend() async throws {
        do {
            _ = try await backend.status()
            return
        } catch {
            status.state = "starting backend"
            startLocalBackendIfNeeded()
        }
        for _ in 0..<120 {
            try? await Task.sleep(nanoseconds: 500_000_000)
            do {
                _ = try await backend.status()
                return
            } catch {
                continue
            }
        }
        throw BackendClientError.invalidPayload("Backend did not become ready on \(backendEndpoint) after 60 seconds")
    }

    private func startLocalBackendIfNeeded() {
        if let backendProcess, backendProcess.isRunning {
            return
        }
        if backendStartInFlight {
            return
        }
        if Date().timeIntervalSince(lastBackendStartAttempt) < 2.0 {
            return
        }
        backendStartInFlight = true
        lastBackendStartAttempt = Date()
        backendBootstrapTask?.cancel()
        backendBootstrapTask = Task { [weak self] in
            await self?.bootstrapLocalBackend()
        }
    }

    private func bootstrapLocalBackend() async {
        defer {
            backendStartInFlight = false
            backendBootstrapTask = nil
            refreshBackendConsoleLogs()
        }
        let backendDir = defaultBackendDir()
        let logsDir = "\(backendDir)/data/logs"
        try? FileManager.default.createDirectory(
            atPath: logsDir,
            withIntermediateDirectories: true
        )

        let preferredPort = BackendClient.defaultPort
        backendPort = preferredPort
        await backend.setPort(preferredPort)
        if (try? await backend.status()) != nil {
            backendReachable = true
            recordEvent(title: "Connected to backend", detail: "Using existing backend on \(backendEndpoint).", level: .info)
            return
        }

        let listeners = processIDsListening(on: preferredPort)
        if !listeners.isEmpty {
            recordEvent(
                title: "Backend port busy",
                detail: "Port \(preferredPort) is occupied by PID\(listeners.count == 1 ? "" : "s") \(listeners.map(String.init).joined(separator: ", ")). Trying to clear it.",
                level: .warning
            )
            await terminateListeners(listeners, on: preferredPort)
        }

        let selectedPort: Int
        if processIDsListening(on: preferredPort).isEmpty {
            selectedPort = preferredPort
        } else if let alternate = firstOpenBackendPort(after: preferredPort) {
            selectedPort = alternate
            recordEvent(
                title: "Backend port switched",
                detail: "Port \(preferredPort) stayed busy, so JobPilot will use \(selectedPort) for this app session.",
                level: .warning
            )
        } else {
            recordEvent(title: "Backend launch failed", detail: "No open localhost port was found near \(preferredPort).", level: .error)
            return
        }

        backendPort = selectedPort
        await backend.setPort(selectedPort)
        let launch = resolveBackendLaunch(in: backendDir, port: selectedPort)

        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/zsh")
        process.currentDirectoryURL = URL(fileURLWithPath: backendDir)
        process.terminationHandler = { [weak self] process in
            Task { @MainActor [weak self] in
                guard let self else { return }
                if self.backendProcess?.processIdentifier == process.processIdentifier {
                    self.backendProcess = nil
                }
                self.backendStartInFlight = false
                if self.backendBootstrapTask != nil {
                    self.backendBootstrapTask?.cancel()
                    self.backendBootstrapTask = nil
                }
                self.refreshBackendConsoleLogs()
                if process.terminationStatus != 0 {
                    self.recordEvent(
                        title: "Backend process exited",
                        detail: "Exit code \(process.terminationStatus). Check Console Logs for startup details.",
                        level: .error
                    )
                }
            }
        }
        process.arguments = [
            "-lc",
            """
            printf "\\n===== JobPilot backend launch $(date) on port \(selectedPort) =====\\n" >> "\(logsDir)/menubar-backend.stdout.log"
            export PATH="\(launch.pathPrefix):$PATH"
            export JOBPILOT_ENCODER_DEVICE="${JOBPILOT_ENCODER_DEVICE:-cpu}"
            export JOBPILOT_BACKEND_PORT=\(selectedPort)
            export PYTHONUNBUFFERED=1
            exec \(launch.command) \
              >> "\(logsDir)/menubar-backend.stdout.log" \
              2>> "\(logsDir)/menubar-backend.stderr.log"
            """
        ]
        do {
            try process.run()
            backendProcess = process
            recordEvent(title: "Backend launch requested", detail: "\(launch.summary) Endpoint: \(backendEndpoint).", level: .info)
            for _ in 0..<40 {
                if Task.isCancelled { return }
                if (try? await backend.status()) != nil {
                    backendReachable = true
                    recordEvent(title: "Backend ready", detail: "Connected to \(backendEndpoint).", level: .success)
                    return
                }
                if !process.isRunning {
                    return
                }
                try? await Task.sleep(nanoseconds: 500_000_000)
            }
            recordEvent(title: "Backend still starting", detail: "No health response from \(backendEndpoint) yet. Open Console Logs for startup output.", level: .warning)
        } catch {
            status.state = "backend launch failed: \(shortError(error))"
            recordEvent(title: "Backend launch failed", detail: shortError(error), level: .error)
        }
    }

    private func processIDsListening(on port: Int) -> [Int32] {
        let output = shellOutput("lsof -tiTCP:\(port) -sTCP:LISTEN 2>/dev/null || true")
        return output
            .split(whereSeparator: \.isNewline)
            .compactMap { Int32(String($0).trimmingCharacters(in: .whitespacesAndNewlines)) }
            .filter { $0 != getpid() }
    }

    private func terminateListeners(_ pids: [Int32], on port: Int) async {
        let uniquePIDs = Array(Set(pids)).filter { $0 != getpid() }
        guard !uniquePIDs.isEmpty else { return }
        uniquePIDs.forEach { _ = kill($0, SIGTERM) }
        for _ in 0..<10 {
            if processIDsListening(on: port).isEmpty {
                return
            }
            try? await Task.sleep(nanoseconds: 200_000_000)
        }
        processIDsListening(on: port).forEach { _ = kill($0, SIGKILL) }
        for _ in 0..<5 {
            if processIDsListening(on: port).isEmpty {
                return
            }
            try? await Task.sleep(nanoseconds: 200_000_000)
        }
    }

    private func firstOpenBackendPort(after preferredPort: Int) -> Int? {
        let lower = min(max(preferredPort + 1, 1024), 65_535)
        let upper = min(lower + 100, 65_535)
        guard lower <= upper else { return nil }
        return (lower...upper).first { processIDsListening(on: $0).isEmpty }
    }

    private func shellOutput(_ command: String) -> String {
        let process = Process()
        let pipe = Pipe()
        process.executableURL = URL(fileURLWithPath: "/bin/zsh")
        process.arguments = ["-lc", command]
        process.standardOutput = pipe
        process.standardError = Pipe()
        do {
            try process.run()
            process.waitUntilExit()
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            return String(data: data, encoding: .utf8) ?? ""
        } catch {
            return ""
        }
    }

    private func defaultBackendDir() -> String {
        // Preference order: explicit env var, common home-relative checkout locations, bundle sibling.
        if let fromEnv = ProcessInfo.processInfo.environment["JOBPILOT_BACKEND_DIR"], !fromEnv.isEmpty {
            return fromEnv
        }
        let fm = FileManager.default
        let home = fm.homeDirectoryForCurrentUser
        let homeCandidates = [
            home.appendingPathComponent("Desktop/Resume/Application-Automation/jobpilot"),
            home.appendingPathComponent("Desktop/JobPilot-main/jobpilot")
        ]
        for candidate in homeCandidates where fm.fileExists(atPath: candidate.appendingPathComponent("backend/main.py").path) {
            return candidate.path
        }
        // Fallback: search upward from the app bundle for a sibling `jobpilot` dir.
        var dir = Bundle.main.bundleURL.deletingLastPathComponent()
        for _ in 0..<6 {
            let candidate = dir.appendingPathComponent("jobpilot")
            if fm.fileExists(atPath: candidate.appendingPathComponent("backend/main.py").path) {
                return candidate.path
            }
            dir = dir.deletingLastPathComponent()
            if dir.path == "/" { break }
        }
        return homeCandidates[0].path
    }

    private func resolveUvPath() -> String {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let candidates = [
            ProcessInfo.processInfo.environment["UV_BIN"],
            "\(home)/.local/bin/uv",
            "\(home)/.cargo/bin/uv",
            "/opt/homebrew/bin/uv",
            "/usr/local/bin/uv"
        ].compactMap { $0 }
        for candidate in candidates where FileManager.default.isExecutableFile(atPath: candidate) {
            return candidate
        }
        return "uv"
    }

    private func resolveBackendLaunch(in backendDir: String, port: Int) -> (command: String, pathPrefix: String, summary: String) {
        let fm = FileManager.default
        let venvPython = "\(backendDir)/.venv/bin/python3"
        if fm.isExecutableFile(atPath: venvPython) {
            return (
                command: "\"\(venvPython)\" -m uvicorn backend.main:app --host 127.0.0.1 --port \(port)",
                pathPrefix: "\(backendDir)/.venv/bin",
                summary: "Launching backend with repo virtualenv Python on port \(port)."
            )
        }
        let uvPath = resolveUvPath()
        let uvDir = uvPath.contains("/") ? (uvPath as NSString).deletingLastPathComponent : ""
        return (
            command: "\"\(uvPath)\" run uvicorn backend.main:app --host 127.0.0.1 --port \(port)",
            pathPrefix: uvDir,
            summary: "Launching backend with uv at \(uvPath) on port \(port)."
        )
    }

    private func shortError(_ error: Error) -> String {
        let message = (error as? LocalizedError)?.errorDescription ?? String(describing: error)
        return String(message.prefix(90))
    }

    var statusHeadline: String {
        if isRunning {
            if let message = status.currentStageMessage?.trimmingCharacters(in: .whitespacesAndNewlines), !message.isEmpty {
                return message
            }
            if let stage = status.currentStage, !stage.isEmpty {
                return stage.replacingOccurrences(of: "_", with: " ").capitalized
            }
        }
        return status.state.replacingOccurrences(of: "_", with: " ").capitalized
    }
}

enum TimelineLevel: String {
    case info
    case success
    case warning
    case error
}

struct TimelineEvent: Identifiable {
    let id = UUID()
    let title: String
    let detail: String?
    let level: TimelineLevel
    let date: Date

    var timeText: String {
        let formatter = DateFormatter()
        formatter.timeStyle = .medium
        formatter.dateStyle = .none
        return formatter.string(from: date)
    }
}

struct PendingAlarm: Identifiable {
    let id = UUID()
    let token: String
    let question: String
    let fieldType: String
    let options: [String]
}

struct PendingClassifierReview: Identifiable {
    let id = UUID()
    let token: String
    let company: String?
    let title: String?
    let jobURL: String?
    let classifierScore: Double?
    let location: String?
    let descriptionPreview: String?
    let descriptionText: String?
    let fitDecisionSummary: String?

    enum CodingKeys: String, CodingKey {
        case token
        case company
        case title
        case jobURL = "job_url"
        case classifierScore = "classifier_score"
        case location
        case descriptionPreview = "description_preview"
        case descriptionText = "description_text"
        case fitDecisionSummary = "fit_decision_summary"
    }
}

extension PendingClassifierReview: Decodable {}

struct PendingManualTakeover: Identifiable {
    let id = UUID()
    let token: String
    let company: String?
    let title: String?
    let jobURL: String?
    let reason: String?
    let currentURL: String?
    let allowButtonNameRegistration: Bool?

    enum CodingKeys: String, CodingKey {
        case token
        case company
        case title
        case jobURL = "job_url"
        case reason
        case currentURL = "current_url"
        case allowButtonNameRegistration = "allow_button_name_registration"
    }
}

extension PendingManualTakeover: Decodable {}

struct PendingApproval: Identifiable {
    let id = UUID()
    let token: String
    let company: String?
    let title: String?
    let jobURL: String?
    let classifierScore: Double?
    let descriptionText: String?
    let resumePath: String?
    let coverLetterPath: String?
    var fieldAnswers: [ReviewField]
    let validationWarnings: [ValidationWarning]
    let missingRequiredFields: [String]
    let dryRun: Bool

    enum CodingKeys: String, CodingKey {
        case token
        case company
        case title
        case jobURL = "job_url"
        case classifierScore = "classifier_score"
        case descriptionText = "description_text"
        case resumePath = "resume_path"
        case coverLetterPath = "cover_letter_path"
        case fieldAnswers = "field_answers"
        case validationWarnings = "validation_warnings"
        case missingRequiredFields = "missing_required_fields"
        case dryRun = "dry_run"
    }
}

extension PendingApproval: Decodable {}

struct ValidationWarning: Identifiable {
    let id = UUID()
    let level: String
    let label: String
    let message: String

    init(payload: [String: Any]) {
        self.level = payload["level"] as? String ?? "warning"
        self.label = payload["label"] as? String ?? "Field"
        self.message = payload["message"] as? String ?? ""
    }
}

extension ValidationWarning: Decodable {
    private enum CodingKeys: String, CodingKey {
        case level
        case label
        case message
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.level = try container.decodeIfPresent(String.self, forKey: .level) ?? "warning"
        self.label = try container.decodeIfPresent(String.self, forKey: .label) ?? "Field"
        self.message = try container.decodeIfPresent(String.self, forKey: .message) ?? ""
    }
}

struct ReviewField: Identifiable {
    var id: String { key }
    let key: String
    let label: String
    var value: String
    let required: Bool
    let fieldType: String
    let editable: Bool

    init(payload: [String: Any]) {
        let label = payload["label"] as? String ?? ""
        self.key = payload["key"] as? String ?? label
        self.label = label
        if let value = payload["value"] {
            self.value = String(describing: value)
        } else {
            self.value = ""
        }
        self.required = payload["required"] as? Bool ?? false
        self.fieldType = payload["field_type"] as? String ?? "text"
        self.editable = payload["editable"] as? Bool ?? true
    }
}

extension ReviewField: Decodable {
    private enum CodingKeys: String, CodingKey {
        case key
        case label
        case value
        case required
        case fieldType = "field_type"
        case editable
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let label = try container.decodeIfPresent(String.self, forKey: .label) ?? ""
        self.key = try container.decodeIfPresent(String.self, forKey: .key) ?? label
        self.label = label
        self.value = try container.decodeIfPresent(String.self, forKey: .value) ?? ""
        self.required = try container.decodeIfPresent(Bool.self, forKey: .required) ?? false
        self.fieldType = try container.decodeIfPresent(String.self, forKey: .fieldType) ?? "text"
        self.editable = try container.decodeIfPresent(Bool.self, forKey: .editable) ?? true
    }
}

struct BackendStatus: Codable {
    var state: String = "idle"
    var currentJob: String?
    var currentStage: String?
    var currentStageMessage: String?
    var today: Int = 0
    var week: Int = 0
    var allTime: Int = 0
    var last10: [ApplicationRow] = []

    enum CodingKeys: String, CodingKey {
        case state
        case currentJob = "current_job"
        case currentStage = "current_stage"
        case currentStageMessage = "current_stage_message"
        case today
        case week
        case allTime = "all_time"
        case last10 = "last_10"
    }
}

struct BackendSettings: Codable {
    var liveSubmitEnabled: Bool = false
    var autoSubmitWithoutApproval: Bool = false
    var finalReviewRequired: Bool = true
    var classifierAutoPassWhenAboveThreshold: Bool = false
    var liveModeEnabled: Bool = false
    var dryRun: Bool = true
    var browserHeadless: Bool = false
    var browserPersistent: Bool = true
    var browserUserDataDir: String = ""

    enum CodingKeys: String, CodingKey {
        case liveSubmitEnabled = "live_submit_enabled"
        case autoSubmitWithoutApproval = "auto_submit_without_approval"
        case finalReviewRequired = "final_review_required"
        case classifierAutoPassWhenAboveThreshold = "classifier_auto_pass_when_above_threshold"
        case liveModeEnabled = "live_mode_enabled"
        case dryRun = "dry_run"
        case browserHeadless = "browser_headless"
        case browserPersistent = "browser_persistent"
        case browserUserDataDir = "browser_user_data_dir"
    }
}

struct BackendLogs: Codable {
    var logsDir: String = ""
    var stdout: String = ""
    var stderr: String = ""

    var combinedText: String {
        """
        Logs directory:
        \(logsDir)

        ===== STDOUT =====
        \(stdout)

        ===== STDERR =====
        \(stderr)
        """
    }

    enum CodingKeys: String, CodingKey {
        case logsDir = "logs_dir"
        case stdout
        case stderr
    }
}

struct BackendConsoleLogs {
    var logsDir: String = ""
    var stdout: String = ""
    var stderr: String = ""

    var combinedText: String {
        """
        Logs directory:
        \(logsDir)

        ===== BACKEND STDOUT =====
        \(stdout)

        ===== BACKEND STDERR =====
        \(stderr)
        """
    }
}

struct RunRow: Codable, Identifiable {
    let id: Int
    let careerPageURL: String
    let startedAt: String
    let endedAt: String?
    let jobsSeen: Int
    let jobsPassed: Int
    let jobsApplied: Int
    let status: String?

    enum CodingKeys: String, CodingKey {
        case id
        case careerPageURL = "career_page_url"
        case startedAt = "started_at"
        case endedAt = "ended_at"
        case jobsSeen = "jobs_seen"
        case jobsPassed = "jobs_passed"
        case jobsApplied = "jobs_applied"
        case status
    }
}

struct ApplicationRow: Codable, Identifiable {
    var id: String { jobURL }
    let company: String?
    let title: String?
    let jobURL: String
    let appliedAt: String?
    let submitted: Int
    let decision: String?
    let error: String?
    let resumePath: String?
    let coverLetterPath: String?
    let classifierScore: Double?
    let location: String?
    let applicationType: String?
    let resumeLatexCode: String?
    // Per-mode attempt status. The History window groups rows by attempt mode
    // (DRY RUN / REAL SUBMITS); a row is "green" in a section only when its
    // per-mode outcome is success and the per-mode error is nil.
    let dryRunOutcome: String?
    let dryRunCompletedAt: String?
    let dryRunError: String?
    let realSubmitOutcome: String?
    let realSubmitCompletedAt: String?
    let realSubmitError: String?

    var isApplied: Bool {
        submitted == 1 || appliedAt != nil
    }

    /// True when this row succeeded in a dry run and is not currently in an error state.
    var dryRunSucceeded: Bool {
        let success = Set(["dry_run_complete", "completed_with_deferred"])
        guard let outcome = dryRunOutcome?.lowercased(), success.contains(outcome) else { return false }
        return (dryRunError ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    /// True when this row was actually submitted live.
    var realSubmitSucceeded: Bool {
        let success = Set(["submitted", "submitted_unconfirmed"])
        guard let outcome = realSubmitOutcome?.lowercased(), success.contains(outcome) else { return false }
        return (realSubmitError ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    /// True if there is any prior dry-run attempt recorded.
    var hasDryRunAttempt: Bool {
        (dryRunOutcome ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty == false
    }

    /// True if there is any prior real-submit attempt recorded.
    var hasRealSubmitAttempt: Bool {
        (realSubmitOutcome ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty == false
    }

    var displayCompany: String {
        if let company, !company.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return company
        }
        return Self.companyName(from: jobURL) ?? "Unknown Company"
    }

    var hasGeneratedFiles: Bool {
        resumePath != nil || coverLetterPath != nil
    }

    var decisionLabel: String {
        guard let decision else { return "Not decided" }
        switch decision {
        case "pass": return "Eligible"
        case "fail": return "Filtered out"
        case "human_fail": return "Rejected in review"
        case "manual_review_required": return "Needs manual review"
        case "manual_auth_required": return "Needs sign-in"
        case "blocked_credentials": return "Blocked by credentials"
        case "provider_backoff": return "Waiting for retry"
        case "failed_transient": return "Temporary failure"
        case "duplicate": return "Duplicate"
        case "error": return "Error"
        default:
            return decision
                .split(separator: "_")
                .map { $0.capitalized }
                .joined(separator: " ")
        }
    }

    var displayStatus: String {
        if let error, !error.isEmpty {
            return "\(decisionLabel): \(error)"
        }
        if isApplied {
            return "Applied"
        }
        if hasGeneratedFiles {
            return "Prepared files; not submitted"
        }
        if decision != nil {
            return decisionLabel
        }
        return "Recorded"
    }

    var needsAttention: Bool {
        if let error, !error.isEmpty {
            return true
        }
        return [
            "manual_review_required",
            "manual_auth_required",
            "blocked_credentials",
            "provider_backoff",
            "failed_transient",
            "error"
        ].contains(decision ?? "")
    }

    var prepared: Bool {
        hasGeneratedFiles
    }

    var badgeText: String {
        if needsAttention { return "Attention" }
        if isApplied { return "Applied" }
        if hasGeneratedFiles { return "Prepared" }
        if decision == "fail" || decision == "human_fail" { return "Filtered" }
        if decision == "duplicate" { return "Duplicate" }
        return "Recorded"
    }

    var scoreText: String? {
        guard let classifierScore else { return nil }
        return String(format: "Score: %.2f", classifierScore)
    }

    enum CodingKeys: String, CodingKey {
        case company
        case title
        case jobURL = "job_url"
        case appliedAt = "applied_at"
        case submitted
        case decision
        case error
        case resumePath = "resume_path"
        case coverLetterPath = "cover_letter_path"
        case classifierScore = "classifier_score"
        case location
        case applicationType = "application_type"
        case resumeLatexCode = "resume_latex_code"
        case dryRunOutcome = "dry_run_outcome"
        case dryRunCompletedAt = "dry_run_completed_at"
        case dryRunError = "dry_run_error"
        case realSubmitOutcome = "real_submit_outcome"
        case realSubmitCompletedAt = "real_submit_completed_at"
        case realSubmitError = "real_submit_error"
    }

    private static func companyName(from jobURL: String) -> String? {
        guard let url = URL(string: jobURL) else { return nil }
        let host = (url.host ?? "").lowercased()
        let segments = url.path.split(separator: "/").map(String.init)
        let pathFirstHosts = ["greenhouse.io", "workable.com", "ashbyhq.com", "lever.co", "smartrecruiters.com"]
        if pathFirstHosts.contains(where: { host == $0 || host.hasSuffix("." + $0) }) {
            for segment in segments {
                let lowered = segment.lowercased()
                if !["jobs", "job", "careers", "career", "apply", "en-us", "en"].contains(lowered), Int(lowered) == nil {
                    return humanizeCompanySlug(segment)
                }
            }
        }
        let labels = host.split(separator: ".").map(String.init)
        let ignored = Set([
            "www", "jobs", "careers", "apply", "boards", "job-boards",
            "greenhouse", "workable", "ashbyhq", "lever", "smartrecruiters",
            "com", "io", "co", "org", "net"
        ])
        for label in labels where !ignored.contains(label) && !label.hasPrefix("wd") {
            return humanizeCompanySlug(label)
        }
        return nil
    }

    private static func humanizeCompanySlug(_ value: String) -> String? {
        var text = value.trimmingCharacters(in: CharacterSet(charactersIn: "/ "))
        for suffix in ["-inc", "_inc", ".inc", "-llc", "_llc", ".llc", "-ltd", "_ltd", ".ltd"] where text.lowercased().hasSuffix(suffix) {
            text.removeLast(suffix.count)
            break
        }
        let words = text
            .replacingOccurrences(of: "_", with: "-")
            .replacingOccurrences(of: ".", with: "-")
            .split(separator: "-")
            .map(String.init)
        guard !words.isEmpty else { return nil }
        return words.map { word in
            word.count <= 3 ? word.uppercased() : word.prefix(1).uppercased() + String(word.dropFirst())
        }.joined(separator: " ")
    }
}
