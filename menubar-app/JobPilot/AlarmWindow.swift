import AppKit
import SwiftUI

struct AlarmWindow: View {
    @EnvironmentObject private var state: AppState

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("This job asks:")
                    .font(.headline)
                Spacer()
                AlarmLevelBadge(level: state.alarmLevelLabel, snoozed: state.alarmIsSnoozed)
            }
            if state.alarmPending && !state.alarmField.isEmpty {
                Text(state.alarmField)
                    .font(.caption)
                    .bold()
                    .foregroundStyle(.orange)
            }
            Text(alarmQuestionText)
            if let alarm = state.alarm, !alarm.options.isEmpty {
                optionAnswerView(alarm)
            } else {
                TextEditor(text: $state.alarmAnswer)
                    .frame(minHeight: 120)
            }
            if state.settings.liveModeEnabled {
                Text("Watch browser: type the answer directly into the focused field in the automation browser, then press \"Use browser input\". The app will learn from what you typed.")
                    .font(.caption2)
                    .foregroundStyle(.blue)
            }
            HStack(spacing: 8) {
                Button("Silence") {
                    state.stopAlarm()
                }
                Button(state.alarmIsSnoozed ? "Snoozed" : "Snooze 60s") {
                    state.snoozeAlarm()
                }
                .disabled(state.alarmIsSnoozed)
                Spacer()
                Button("Show Automation Browser") {
                    Task { await state.focusAutomationBrowser() }
                }
                .help("Bring the active automation browser window to the front.")
                if state.settings.liveModeEnabled {
                    Button("Use browser input") {
                        Task { await state.useBrowserAnswer() }
                    }
                    .help("Read the value from the currently focused field in the automation browser.")
                }
                Button("Submit") {
                    Task {
                        await state.submitAlarm()
                    }
                }
                .disabled(state.alarmAnswer.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && !state.settings.liveModeEnabled)
                .help(state.alarmAnswer.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && !state.settings.liveModeEnabled
                      ? "Type an answer here or switch on Watch browser and use the browser input."
                      : "Save this answer and let the run continue.")
                .keyboardShortcut(.defaultAction)
            }
        }
        .padding()
        .frame(width: 560, height: 420)
    }

    private var alarmQuestionText: String {
        let text = (state.alarm?.question ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        return text.isEmpty ? "No field label provided. Open the browser to inspect." : text
    }

    @ViewBuilder
    private func optionAnswerView(_ alarm: PendingAlarm) -> some View {
        let type = alarm.fieldType.lowercased()
        if type == "checkbox" {
            ScrollView {
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(alarm.options, id: \.self) { option in
                        Toggle(option, isOn: checkboxBinding(for: option))
                    }
                }
                .padding(10)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(minHeight: 120, maxHeight: 170)
            .background(Color.secondary.opacity(0.08))
            .clipShape(RoundedRectangle(cornerRadius: 8))
        } else {
            Picker("Answer", selection: singleOptionBinding()) {
                Text("Select...").tag("")
                ForEach(alarm.options, id: \.self) { option in
                    Text(option).tag(option)
                }
            }
            .pickerStyle(.menu)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func checkboxBinding(for option: String) -> Binding<Bool> {
        Binding(
            get: {
                selectedOptions.contains(option)
            },
            set: { isSelected in
                var selected = selectedOptions
                if isSelected {
                    if !selected.contains(option) {
                        selected.append(option)
                    }
                } else {
                    selected.removeAll { $0 == option }
                }
                state.alarmAnswer = selected.joined(separator: ", ")
            }
        )
    }

    private func singleOptionBinding() -> Binding<String> {
        Binding(
            get: {
                state.alarmAnswer
            },
            set: { newValue in
                state.alarmAnswer = newValue
            }
        )
    }

    private var selectedOptions: [String] {
        state.alarmAnswer
            .split(separator: ",")
            .map { String($0).trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
    }
}

/// Tiny pill showing the current alarm escalation level.
struct AlarmLevelBadge: View {
    let level: String
    let snoozed: Bool

    var body: some View {
        HStack(spacing: 4) {
            Image(systemName: snoozed ? "zzz" : icon)
            Text(snoozed ? "Snoozed" : level)
                .font(.caption2)
                .bold()
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 4)
        .background(color.opacity(0.14))
        .foregroundStyle(color)
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    private var icon: String {
        switch level {
        case "Critical": return "bell.and.waves.left.and.right.fill"
        case "Urgent": return "bell.fill"
        case "Elevated": return "bell"
        default: return "bell.slash"
        }
    }

    private var color: Color {
        switch level {
        case "Critical": return .red
        case "Urgent": return .orange
        case "Elevated": return .yellow
        case "Gentle": return .blue
        default: return .gray
        }
    }
}

struct ClassifierReviewWindow: View {
    @EnvironmentObject private var state: AppState

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Classifier Review")
                .font(.headline)
            Text(state.classifierReview?.title ?? "Untitled role")
                .font(.title3)
                .bold()
            Text(state.classifierReview?.company ?? "")
                .foregroundStyle(.secondary)
            if let location = state.classifierReview?.location, !location.isEmpty {
                Text(location)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            if let url = state.classifierReview?.jobURL {
                Text(url)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
            }
            if let score = state.classifierReview?.classifierScore {
                Text("Classifier score: \(score, specifier: "%.2f")")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            if let summary = state.classifierReview?.fitDecisionSummary, !summary.isEmpty {
                Text(summary)
                    .font(.caption)
                    .foregroundStyle(summary.contains("blocked") ? .orange : .green)
                    .textSelection(.enabled)
            }
            Divider()
            ScrollView {
                Text(state.classifierReview?.descriptionText ?? state.classifierReview?.descriptionPreview ?? "")
                    .font(.caption)
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            Divider()
            HStack(spacing: 8) {
                Button("Silence") {
                    state.stopAlarm()
                }
                Button(state.alarmIsSnoozed ? "Snoozed" : "Snooze 60s") {
                    state.snoozeAlarm()
                }
                .disabled(state.alarmIsSnoozed)
                Button("Mark Fail") {
                    Task { await state.respondToClassifierReview(false) }
                }
                Spacer()
                AlarmLevelBadge(level: state.alarmLevelLabel, snoozed: state.alarmIsSnoozed)
                Button("Show Automation Browser") {
                    Task { await state.focusAutomationBrowser() }
                }
                Button("Pass to Next Step") {
                    Task { await state.respondToClassifierReview(true) }
                }
                .keyboardShortcut(.defaultAction)
            }
        }
        .padding()
        .frame(width: 680, height: 560)
    }
}

struct ManualTakeoverWindow: View {
    @EnvironmentObject private var state: AppState
    @State private var selectedButtonType: String? = nil
    @State private var buttonText: String = ""
    @State private var step: Int = 1

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Manual Browser Takeover")
                .font(.headline)
            Text(state.manualTakeover?.title ?? "Application needs help")
                .font(.title3)
                .bold()
            if let company = state.manualTakeover?.company {
                Text(company)
                    .foregroundStyle(.secondary)
            }
            Text(state.manualTakeover?.reason ?? "Finish the blocking step in the visible browser, then continue.")
                .font(.caption)
                .foregroundStyle(.orange)
                .textSelection(.enabled)
            if let currentURL = state.manualTakeover?.currentURL {
                Text(currentURL)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
            }
            Divider()

            // Two-step button registration flow
            if state.manualTakeover?.allowButtonNameRegistration == true && step == 1 {
                VStack(alignment: .leading, spacing: 10) {
                    Text("Which type of button is this?")
                        .font(.subheadline)
                        .bold()
                    HStack(spacing: 10) {
                        Button(action: {
                            selectedButtonType = "submit"
                            step = 2
                        }) {
                            Text("SUBMIT Button")
                                .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(.bordered)
                        Button(action: {
                            selectedButtonType = "next"
                            step = 2
                        }) {
                            Text("NEXT Button")
                                .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(.bordered)
                    }
                }
            } else if state.manualTakeover?.allowButtonNameRegistration == true && step == 2 {
                VStack(alignment: .leading, spacing: 10) {
                    Text("What text does the button show?")
                        .font(.subheadline)
                        .bold()
                    HStack(spacing: 8) {
                        TextField("e.g., Apply now, Continue, Next step", text: $buttonText)
                            .textFieldStyle(.roundedBorder)
                            .font(.caption)
                    }
                    HStack(spacing: 8) {
                        Button("Back") {
                            step = 1
                            selectedButtonType = nil
                            buttonText = ""
                        }
                        .buttonStyle(.bordered)
                        Spacer()
                        Button("Register & Continue") {
                            Task {
                                await state.respondToManualTakeover("continue", buttonType: selectedButtonType, buttonText: buttonText)
                            }
                        }
                        .buttonStyle(.borderedProminent)
                        .disabled(buttonText.trimmingCharacters(in: .whitespaces).isEmpty)
                    }
                }
            } else {
                Text("Open the current page in your normal browser and finish login, captcha, or account setup there. Use Continue only if the real application form is now visible back inside JobPilot's automation browser; otherwise finish it manually and skip the job in JobPilot.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            Spacer()
            HStack(spacing: 8) {
                Button("Silence") {
                    state.stopAlarm()
                }
                Button(state.alarmIsSnoozed ? "Snoozed" : "Snooze 60s") {
                    state.snoozeAlarm()
                }
                .disabled(state.alarmIsSnoozed)
                Button("Skip Job") {
                    Task { await state.respondToManualTakeover("skip") }
                }
                Spacer()
                AlarmLevelBadge(level: state.alarmLevelLabel, snoozed: state.alarmIsSnoozed)
                if state.manualTakeover?.allowButtonNameRegistration != true || step < 2 {
                    Button("Open in Default Browser") {
                        Task { await state.openCurrentPageInDefaultBrowser() }
                    }
                    Button("Continue") {
                        Task { await state.respondToManualTakeover("continue") }
                    }
                    .keyboardShortcut(.defaultAction)
                }
            }
        }
        .padding()
        .frame(width: 620, height: state.manualTakeover?.allowButtonNameRegistration == true ? 480 : 380)
    }
}

struct ApprovalWindow: View {
    @EnvironmentObject private var state: AppState
    @State private var isEditing = false
    @State private var showRequiredOnly = false
    @State private var showBlankOnly = false

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            header
            Divider()
            warnings
            Divider()
            documents
            Divider()
            descriptionSection
            Divider()
            fieldList
            Divider()
            HStack(spacing: 8) {
                Button("Silence") {
                    state.stopAlarm()
                }
                Button(state.alarmIsSnoozed ? "Snoozed" : "Snooze 60s") {
                    state.snoozeAlarm()
                }
                .disabled(state.alarmIsSnoozed)
                Button("Skip") {
                    Task { await state.respondToApproval(false) }
                }
                Spacer()
                AlarmLevelBadge(level: state.alarmLevelLabel, snoozed: state.alarmIsSnoozed)
                Button("Show Automation Browser") {
                    Task { await state.focusAutomationBrowser() }
                }
                Button(isEditing ? "Done Editing" : "Edit Answer") {
                    isEditing.toggle()
                }
                Button(state.approval?.dryRun == true ? "Approve Dry Run" : "Real Submit") {
                    Task { await state.respondToApproval(true) }
                }
                .disabled(hasBlockingWarnings)
                .keyboardShortcut(.defaultAction)
            }
        }
        .padding()
        .frame(width: 800, height: 700)
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(state.approval?.dryRun == true ? "Final Review (Dry Run)" : "Final Review")
                .font(.headline)
                .foregroundStyle(state.approval?.dryRun == true ? Color.primary : Color.red)
            Text(state.approval?.title ?? "Untitled role")
                .font(.title3)
                .bold()
            Text(state.approval?.company ?? "")
                .foregroundStyle(.secondary)
            if let url = state.approval?.jobURL {
                Text(url)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
            }
            if let score = state.approval?.classifierScore {
                Text("Classifier score: \(score, specifier: "%.2f")")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Text(state.approval?.dryRun == true ? "Dry run: final submit will be skipped." : "Real submit: approval will submit the real form.")
                .font(.caption)
                .foregroundStyle(state.approval?.dryRun == true ? Color.secondary : Color.red)
        }
    }

    private var warnings: some View {
        VStack(alignment: .leading, spacing: 6) {
            let warnings = state.approval?.validationWarnings ?? []
            let localIssues = (state.approval?.fieldAnswers ?? []).compactMap { field -> String? in
                guard let message = validationMessage(for: field) else { return nil }
                return "\(field.label.isEmpty ? field.key : field.label): \(message)"
            }
            if warnings.isEmpty && localIssues.isEmpty {
                Label("No required-field warnings detected.", systemImage: "checkmark.circle")
                    .font(.caption)
                    .foregroundStyle(.green)
            } else {
                ForEach(warnings) { warning in
                    Label(warning.message, systemImage: warning.level.lowercased() == "error" ? "xmark.octagon" : "exclamationmark.triangle")
                        .font(.caption)
                        .foregroundStyle(warning.level.lowercased() == "error" ? .red : .orange)
                }
                ForEach(localIssues, id: \.self) { issue in
                    Label(issue, systemImage: "xmark.octagon")
                        .font(.caption)
                        .foregroundStyle(.red)
                }
            }
        }
    }

    private var documents: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Documents").font(.headline)
            if let resume = state.approval?.resumePath {
                DocumentRow(label: "Resume PDF", path: resume)
            }
            if let cover = state.approval?.coverLetterPath, !cover.isEmpty {
                DocumentRow(label: "Cover letter PDF", path: cover)
            } else {
                Text("Cover letter: not requested")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private var descriptionSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Job Description").font(.headline)
            ScrollView {
                Text(state.approval?.descriptionText ?? "")
                    .font(.caption)
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(minHeight: 120, maxHeight: 180)
        }
    }

    private var fieldList: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Planned Answers").font(.headline)
            HStack {
                Toggle("Required only", isOn: $showRequiredOnly)
                    .toggleStyle(.checkbox)
                Toggle("Blank only", isOn: $showBlankOnly)
                    .toggleStyle(.checkbox)
                Spacer()
            }
            .font(.caption)
            ScrollView {
                VStack(alignment: .leading, spacing: 10) {
                    ForEach(filteredFields) { field in
                        ReviewFieldRow(field: field, isEditing: isEditing)
                    }
                }
                .padding(.trailing, 8)
            }
        }
    }

    private var filteredFields: [ReviewField] {
        (state.approval?.fieldAnswers ?? []).filter { field in
            if showRequiredOnly && !field.required { return false }
            if showBlankOnly && !field.value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { return false }
            return true
        }
    }

    private var hasBlockingWarnings: Bool {
        let fields = state.approval?.fieldAnswers ?? []
        let backendErrors = (state.approval?.validationWarnings ?? []).contains { $0.level.lowercased() == "error" }
        return backendErrors || fields.contains { validationMessage(for: $0) != nil }
    }
}

private struct DocumentRow: View {
    let label: String
    let path: String

    var body: some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text(label).font(.caption).bold()
                Text(path).font(.caption2).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer()
            Button("Open") {
                NSWorkspace.shared.open(URL(fileURLWithPath: path))
            }
        }
    }
}

private struct ReviewFieldRow: View {
    @EnvironmentObject private var state: AppState
    let field: ReviewField
    let isEditing: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack {
                Text(field.label.isEmpty ? field.key : field.label)
                    .font(.caption)
                    .bold()
                Text(field.required ? "Required" : "Optional")
                    .font(.caption2)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .background(field.required ? Color.red.opacity(0.12) : Color.gray.opacity(0.12))
                    .clipShape(RoundedRectangle(cornerRadius: 4))
                Text(field.fieldType)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                Spacer()
            }
            if isEditing && field.editable {
                TextEditor(
                    text: Binding(
                        get: { field.value },
                        set: { state.updateApprovalField(field, value: $0) }
                    )
                )
                .font(.caption)
                .frame(minHeight: field.value.count > 120 ? 72 : 34)
                .overlay(
                    RoundedRectangle(cornerRadius: 6)
                        .stroke(Color.gray.opacity(0.25))
                )
            } else {
                Text(field.value.isEmpty ? "No answer planned" : field.value)
                    .font(.caption)
                    .textSelection(.enabled)
                    .foregroundStyle(field.value.isEmpty ? .secondary : .primary)
            }
            if let message = validationMessage(for: field) {
                Label(message, systemImage: "xmark.octagon")
                    .font(.caption2)
                    .foregroundStyle(.red)
            }
        }
        .padding(10)
        .background(Color.gray.opacity(0.06))
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }
}

private func validationMessage(for field: ReviewField) -> String? {
    let value = field.value.trimmingCharacters(in: .whitespacesAndNewlines)
    if value.isEmpty {
        return field.required ? "Required field is blank." : nil
    }
    switch field.fieldType.lowercased() {
    case "email":
        let pattern = #"^[^@\s]+@[^@\s]+\.[^@\s]+$"#
        return value.range(of: pattern, options: .regularExpression) == nil ? "Enter a valid email address." : nil
    case "tel", "phone", "telephone":
        let digits = value.filter(\.isNumber)
        return digits.count < 7 ? "Enter a valid phone number." : nil
    case "date", "datetime-local":
        return isValidReviewDate(value) ? nil : "Enter a valid date."
    default:
        return nil
    }
}

private func isValidReviewDate(_ value: String) -> Bool {
    let formats = ["yyyy-MM-dd", "MM/dd/yyyy", "dd/MM/yyyy", "MMM d, yyyy", "MMMM d, yyyy"]
    return formats.contains { format in
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = format
        formatter.isLenient = false
        return formatter.date(from: value) != nil
    }
}

/// Lets the user tune the multi-channel alarm: which channels are on, and how aggressive
/// the escalation cadence is. Persists to UserDefaults inside AlarmManager.
struct AlarmSettingsWindow: View {
    @EnvironmentObject private var state: AppState
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text("Alarm Settings")
                    .font(.headline)
                Spacer()
                AlarmLevelBadge(level: state.alarmLevelLabel, snoozed: state.alarmIsSnoozed)
            }
            Text("Pick which channels JobPilot uses to get your attention when the app pauses on a job. The alarm escalates over time until you respond or silence it.")
                .font(.caption)
                .foregroundStyle(.secondary)
            Divider()
            Group {
                Toggle("Sound (continuous chime, ramps in volume)", isOn: prefBinding(\.soundEnabled))
                Toggle("Spoken voice (says what JobPilot needs)", isOn: prefBinding(\.speechEnabled))
                Toggle("System notification banner", isOn: prefBinding(\.notificationsEnabled))
                Toggle("Bounce JobPilot in the dock", isOn: prefBinding(\.dockBounceEnabled))
                Toggle("Bring JobPilot to the front when alarm rings", isOn: prefBinding(\.focusAppEnabled))
            }
            .toggleStyle(.switch)
            Divider()
            VStack(alignment: .leading, spacing: 6) {
                Text("Escalation")
                    .font(.caption)
                    .bold()
                Stepper(value: stepperBinding(\.escalateAfterSeconds, range: 5...120, step: 5), in: 5...120, step: 5) {
                    Text("First escalation after \(Int(state.alarmManager.preferences.escalateAfterSeconds))s")
                        .font(.caption)
                }
                Stepper(value: stepperBinding(\.escalationStepSeconds, range: 5...120, step: 5), in: 5...120, step: 5) {
                    Text("Each next step after \(Int(state.alarmManager.preferences.escalationStepSeconds))s")
                        .font(.caption)
                }
                Stepper(value: stepperBinding(\.snoozeSeconds, range: 15...600, step: 15), in: 15...600, step: 15) {
                    Text("Snooze button silences for \(Int(state.alarmManager.preferences.snoozeSeconds))s")
                        .font(.caption)
                }
            }
            Divider()
            HStack {
                Button("Test Alarm") {
                    state.startAlarm(kind: .missingAnswer(question: "Test alarm: this is a preview at full escalation."))
                    state.escalateAlarmNow()
                }
                Button("Stop Test") {
                    state.stopAlarm()
                }
                Spacer()
                Button("Close") {
                    dismiss()
                }
                .keyboardShortcut(.cancelAction)
            }
        }
        .padding()
        .frame(width: 520, height: 480)
    }

    private func prefBinding(_ keyPath: WritableKeyPath<AlarmPreferences, Bool>) -> Binding<Bool> {
        Binding(
            get: { state.alarmManager.preferences[keyPath: keyPath] },
            set: { state.alarmManager.preferences[keyPath: keyPath] = $0 }
        )
    }

    private func stepperBinding(_ keyPath: WritableKeyPath<AlarmPreferences, TimeInterval>, range: ClosedRange<Double>, step: Double) -> Binding<Double> {
        Binding(
            get: { state.alarmManager.preferences[keyPath: keyPath] },
            set: { state.alarmManager.preferences[keyPath: keyPath] = max(range.lowerBound, min(range.upperBound, $0)) }
        )
    }
}
