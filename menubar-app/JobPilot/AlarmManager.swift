import AppKit
import AVFoundation
import Foundation
import UserNotifications

/// Escalating alarm levels.
///
/// Each level turns on more channels and increases sound volume. The alarm starts at
/// `.gentle`, then climbs every few seconds until either the user responds or it hits
/// `.critical`, where it stays (loud sound, spoken reminder, dock bounce, app focus).
enum AlarmLevel: Int, Comparable {
    case gentle = 1    // Single chime + notification banner + activate app.
    case elevated = 2  // Continuous chime at low volume + optional speech + dock request.
    case urgent = 3    // Louder continuous chime + repeating speech + critical dock bounce.
    case critical = 4  // Maximum loudness + speech every few seconds + repeat focus attempts.

    static func < (lhs: AlarmLevel, rhs: AlarmLevel) -> Bool {
        lhs.rawValue < rhs.rawValue
    }

    var soundVolume: Float {
        switch self {
        case .gentle: return 0.45
        case .elevated: return 0.7
        case .urgent: return 0.9
        case .critical: return 1.0
        }
    }

    var soundInterval: TimeInterval {
        switch self {
        case .gentle: return 3.5
        case .elevated: return 2.0
        case .urgent: return 1.2
        case .critical: return 0.9
        }
    }

    var speechInterval: TimeInterval? {
        switch self {
        case .gentle: return nil
        case .elevated: return 18.0
        case .urgent: return 10.0
        case .critical: return 6.0
        }
    }

    var label: String {
        switch self {
        case .gentle: return "Gentle"
        case .elevated: return "Elevated"
        case .urgent: return "Urgent"
        case .critical: return "Critical"
        }
    }
}

/// Which kind of human-in-the-loop moment is triggering the alarm. Controls the
/// spoken text so the user knows at a glance what's waiting.
enum AlarmKind {
    case missingAnswer(question: String)
    case classifierReview(title: String?)
    case manualTakeover(title: String?)
    case finalApproval(title: String?)

    var shortLabel: String {
        switch self {
        case .missingAnswer: return "JobPilot needs an answer from you."
        case .classifierReview: return "JobPilot is waiting for classifier review."
        case .manualTakeover: return "JobPilot needs manual browser takeover."
        case .finalApproval: return "JobPilot is waiting for final approval."
        }
    }

    var spokenPrompt: String {
        switch self {
        case .missingAnswer(let question):
            let trimmed = question.trimmingCharacters(in: .whitespacesAndNewlines)
            if trimmed.isEmpty {
                return "Job Pilot needs an answer from you."
            }
            let shortened = String(trimmed.prefix(140))
            return "Job Pilot needs an answer. \(shortened)"
        case .classifierReview(let title):
            if let title, !title.isEmpty {
                return "Job Pilot is waiting for classifier review of \(title)."
            }
            return "Job Pilot is waiting for classifier review."
        case .manualTakeover(let title):
            if let title, !title.isEmpty {
                return "Job Pilot needs manual browser takeover for \(title)."
            }
            return "Job Pilot needs manual browser takeover."
        case .finalApproval(let title):
            if let title, !title.isEmpty {
                return "Job Pilot is waiting for final approval on \(title)."
            }
            return "Job Pilot is waiting for final approval."
        }
    }

    var notificationTitle: String {
        switch self {
        case .missingAnswer: return "JobPilot: Missing answer"
        case .classifierReview: return "JobPilot: Classifier review"
        case .manualTakeover: return "JobPilot: Manual takeover"
        case .finalApproval: return "JobPilot: Final approval"
        }
    }

    var notificationBody: String {
        switch self {
        case .missingAnswer(let question):
            return question.isEmpty ? "The app paused on a required field." : question
        case .classifierReview(let title):
            return title ?? "Review whether a passed job should continue."
        case .manualTakeover(let title):
            return title ?? "Finish login, captcha, or another blocker in the browser."
        case .finalApproval(let title):
            return title ?? "Review documents and answers before the app submits."
        }
    }
}

/// User-tunable alarm preferences. Stored in UserDefaults so they survive restarts.
struct AlarmPreferences: Codable, Equatable {
    var soundEnabled: Bool = true
    var speechEnabled: Bool = true
    var notificationsEnabled: Bool = true
    var dockBounceEnabled: Bool = true
    var focusAppEnabled: Bool = true
    /// Seconds before climbing from gentle → elevated.
    var escalateAfterSeconds: TimeInterval = 12
    /// Seconds between successive escalations (elevated→urgent→critical).
    var escalationStepSeconds: TimeInterval = 15
    /// How long "Snooze" quiets the alarm.
    var snoozeSeconds: TimeInterval = 60

    static let storageKey = "JobPilotAlarmPreferences.v1"

    static func load() -> AlarmPreferences {
        guard let data = UserDefaults.standard.data(forKey: storageKey) else { return AlarmPreferences() }
        return (try? JSONDecoder().decode(AlarmPreferences.self, from: data)) ?? AlarmPreferences()
    }

    func save() {
        if let data = try? JSONEncoder().encode(self) {
            UserDefaults.standard.set(data, forKey: Self.storageKey)
        }
    }
}

/// Coordinates a multi-channel escalating alarm for JobPilot.
///
/// Channels: system sound (continuous, volume ramps with level), NSSpeechSynthesizer
/// voice, UserNotifications banner, NSApp.requestUserAttention dock bounce, and app
/// activation. Every channel can be toggled independently in settings.
///
/// Lifecycle:
/// - `start(kind:)` begins the alarm at `.gentle`.
/// - `escalate()` (invoked automatically by a timer) moves up one level.
/// - `snooze()` silences audio for `snoozeSeconds`, leaving the alarm armed.
/// - `stop()` stops every channel and resets state.
@MainActor
final class AlarmManager: ObservableObject {
    @Published private(set) var isRinging: Bool = false
    @Published private(set) var level: AlarmLevel = .gentle
    @Published private(set) var isSnoozed: Bool = false
    @Published var preferences: AlarmPreferences {
        didSet {
            preferences.save()
            applyPreferenceSideEffects()
        }
    }

    private var currentKind: AlarmKind?
    private var startedAt: Date?
    private var soundTimer: Timer?
    private var escalationTimer: Timer?
    private var speechTimer: Timer?
    private var dockBounceRequestID: Int?
    private var snoozeTimer: Timer?

    private let synthesizer = NSSpeechSynthesizer()
    /// Cached NSSound instances by level so we don't reload from disk every tick.
    private var cachedSounds: [AlarmLevel: NSSound] = [:]
    /// Whether notification authorization has already been requested this session.
    private var requestedNotificationAuth = false

    init() {
        self.preferences = AlarmPreferences.load()
        // Pre-warm cached sounds so the first trigger has no disk hitch.
        for level in [AlarmLevel.gentle, .elevated, .urgent, .critical] {
            _ = sound(for: level)
        }
        // Ask for notification permission up front; macOS will silently no-op on
        // later calls if the user denied.
        requestNotificationAuthIfNeeded()
    }

    deinit {
        // Timers retain self; make sure they're cancelled if this ever deallocates.
        soundTimer?.invalidate()
        escalationTimer?.invalidate()
        speechTimer?.invalidate()
        snoozeTimer?.invalidate()
    }

    // MARK: - Public control

    /// Begin a fresh alarm for the given moment. Safe to call while already ringing —
    /// swaps the `kind` in place and resets escalation timers so the new prompt is heard.
    func start(kind: AlarmKind) {
        currentKind = kind
        startedAt = Date()
        isRinging = true
        level = .gentle
        isSnoozed = false
        cancelTimers()
        // Fire the gentle channels immediately: one chime, one notification, bring app forward.
        playSoundTick()
        postNotification()
        focusAppIfAllowed()
        scheduleSoundLoop()
        scheduleEscalation()
    }

    /// Full stop: every channel goes silent, level resets.
    func stop() {
        cancelTimers()
        stopSound()
        stopSpeech()
        cancelDockBounce()
        isRinging = false
        isSnoozed = false
        level = .gentle
        currentKind = nil
        startedAt = nil
    }

    /// Silence audio + speech for `preferences.snoozeSeconds`. Visual channels (dock
    /// badge, notification) remain in place so the user doesn't forget.
    func snooze() {
        guard isRinging else { return }
        isSnoozed = true
        stopSound()
        stopSpeech()
        snoozeTimer?.invalidate()
        snoozeTimer = Timer.scheduledTimer(withTimeInterval: preferences.snoozeSeconds, repeats: false) { [weak self] _ in
            Task { @MainActor in self?.endSnooze() }
        }
    }

    /// Immediately jump to the most severe level. Useful for tests / debug menus.
    func escalateNow() {
        setLevel(.critical)
    }

    // MARK: - Channel: Sound

    private func scheduleSoundLoop() {
        soundTimer?.invalidate()
        guard preferences.soundEnabled else { return }
        let interval = level.soundInterval
        soundTimer = Timer.scheduledTimer(withTimeInterval: interval, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.playSoundTick() }
        }
    }

    private func playSoundTick() {
        guard preferences.soundEnabled, isRinging, !isSnoozed else { return }
        let sound = sound(for: level)
        sound.volume = level.soundVolume
        sound.stop()
        sound.play()
    }

    private func stopSound() {
        soundTimer?.invalidate()
        soundTimer = nil
        for (_, sound) in cachedSounds {
            sound.stop()
        }
    }

    private func sound(for level: AlarmLevel) -> NSSound {
        if let cached = cachedSounds[level] {
            return cached
        }
        // macOS system sounds live in /System/Library/Sounds. These are guaranteed to
        // exist on any supported macOS version and stay audible against muted apps.
        let name: String
        switch level {
        case .gentle: name = "Glass"
        case .elevated: name = "Ping"
        case .urgent: name = "Sosumi"
        case .critical: name = "Funk"
        }
        let sound = NSSound(named: NSSound.Name(name)) ?? NSSound(named: NSSound.Name("Glass")) ?? NSSound()
        cachedSounds[level] = sound
        return sound
    }

    // MARK: - Channel: Speech

    private func scheduleSpeechLoop() {
        speechTimer?.invalidate()
        guard preferences.speechEnabled, let interval = level.speechInterval else { return }
        // Speak once immediately so the user hears it right as the level climbs.
        speakCurrentPrompt()
        speechTimer = Timer.scheduledTimer(withTimeInterval: interval, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.speakCurrentPrompt() }
        }
    }

    private func speakCurrentPrompt() {
        guard preferences.speechEnabled, isRinging, !isSnoozed, let kind = currentKind else { return }
        if synthesizer.isSpeaking { return }
        synthesizer.startSpeaking(kind.spokenPrompt)
    }

    private func stopSpeech() {
        speechTimer?.invalidate()
        speechTimer = nil
        if synthesizer.isSpeaking {
            synthesizer.stopSpeaking()
        }
    }

    // MARK: - Channel: Notifications

    private func requestNotificationAuthIfNeeded() {
        guard !requestedNotificationAuth else { return }
        requestedNotificationAuth = true
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }
    }

    private func postNotification() {
        guard preferences.notificationsEnabled, let kind = currentKind else { return }
        requestNotificationAuthIfNeeded()
        let content = UNMutableNotificationContent()
        content.title = kind.notificationTitle
        content.body = kind.notificationBody
        content.sound = .default
        content.interruptionLevel = .timeSensitive
        let request = UNNotificationRequest(identifier: "jobpilot.alarm.\(UUID().uuidString)", content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request) { _ in }
    }

    // MARK: - Channel: Dock bounce

    private func bounceDock(critical: Bool) {
        guard preferences.dockBounceEnabled else { return }
        cancelDockBounce()
        dockBounceRequestID = NSApp.requestUserAttention(critical ? .criticalRequest : .informationalRequest)
    }

    private func cancelDockBounce() {
        if let id = dockBounceRequestID {
            NSApp.cancelUserAttentionRequest(id)
            dockBounceRequestID = nil
        }
    }

    // MARK: - Channel: App focus

    private func focusAppIfAllowed() {
        guard preferences.focusAppEnabled else { return }
        NSApp.activate(ignoringOtherApps: true)
    }

    // MARK: - Escalation

    private func scheduleEscalation() {
        escalationTimer?.invalidate()
        escalationTimer = Timer.scheduledTimer(withTimeInterval: preferences.escalateAfterSeconds, repeats: false) { [weak self] _ in
            Task { @MainActor in self?.climb() }
        }
    }

    private func climb() {
        guard isRinging else { return }
        let next: AlarmLevel
        switch level {
        case .gentle: next = .elevated
        case .elevated: next = .urgent
        case .urgent: next = .critical
        case .critical: next = .critical
        }
        setLevel(next)
        if next != .critical {
            // Schedule the next climb.
            escalationTimer = Timer.scheduledTimer(withTimeInterval: preferences.escalationStepSeconds, repeats: false) { [weak self] _ in
                Task { @MainActor in self?.climb() }
            }
        }
    }

    private func setLevel(_ newLevel: AlarmLevel) {
        guard newLevel != level else { return }
        level = newLevel
        // Refresh sound cadence, start speech, and bounce the dock to match the new level.
        scheduleSoundLoop()
        scheduleSpeechLoop()
        if newLevel >= .elevated {
            bounceDock(critical: newLevel >= .urgent)
        }
        if newLevel >= .urgent {
            focusAppIfAllowed()
        }
    }

    // MARK: - Snooze tail

    private func endSnooze() {
        guard isRinging else { return }
        isSnoozed = false
        snoozeTimer?.invalidate()
        snoozeTimer = nil
        scheduleSoundLoop()
        scheduleSpeechLoop()
        playSoundTick()
    }

    // MARK: - Helpers

    private func cancelTimers() {
        soundTimer?.invalidate(); soundTimer = nil
        escalationTimer?.invalidate(); escalationTimer = nil
        speechTimer?.invalidate(); speechTimer = nil
        snoozeTimer?.invalidate(); snoozeTimer = nil
    }

    private func applyPreferenceSideEffects() {
        // When a channel is toggled off while ringing, cancel that channel cleanly so
        // the change takes effect immediately instead of at the next tick.
        if !preferences.soundEnabled { stopSound() }
        if !preferences.speechEnabled { stopSpeech() }
        if !preferences.dockBounceEnabled { cancelDockBounce() }
        // Re-arm timers with updated cadence in case intervals changed.
        if isRinging && !isSnoozed {
            scheduleSoundLoop()
            if level >= .elevated { scheduleSpeechLoop() }
        }
    }
}
