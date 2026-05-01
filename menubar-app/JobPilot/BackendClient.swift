import Foundation

enum BackendClientError: Error, LocalizedError {
    case httpStatus(Int, String)
    case invalidPayload(String)

    var errorDescription: String? {
        switch self {
        case let .httpStatus(status, body):
            return "Backend returned HTTP \(status): \(body)"
        case let .invalidPayload(message):
            return message
        }
    }
}

actor BackendClient {
    static var defaultPort: Int {
        let raw = ProcessInfo.processInfo.environment["JOBPILOT_BACKEND_PORT"] ?? "8765"
        guard let port = Int(raw), (1...65_535).contains(port) else {
            return 8765
        }
        return port
    }

    private var port: Int
    private var baseURL: URL {
        URL(string: "http://127.0.0.1:\(port)")!
    }

    private let decoder = JSONDecoder()

    init(port: Int = BackendClient.defaultPort) {
        self.port = port
    }

    func setPort(_ port: Int) {
        guard (1...65_535).contains(port) else { return }
        self.port = port
    }

    func currentPort() -> Int {
        port
    }

    func start(careerURL: String, limit: Int?, forceReprocess: Bool = false, bypassClassifier: Bool = false) async throws -> Int {
        var request = URLRequest(url: baseURL.appendingPathComponent("run/start"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        var body: [String: Any] = ["career_url": careerURL]
        if let limit {
            body["limit"] = limit
        }
        if forceReprocess {
            body["force_reprocess"] = true
        }
        if bypassClassifier {
            body["bypass_classifier"] = true
        }
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        let (data, response) = try await URLSession.shared.data(for: request)
        try Self.validate(response: response, data: data)
        let payload = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        guard let runID = payload?["run_id"] as? Int else {
            throw BackendClientError.invalidPayload("Backend did not return a run_id")
        }
        return runID
    }

    func stop() async throws {
        var request = URLRequest(url: baseURL.appendingPathComponent("run/stop"))
        request.httpMethod = "POST"
        let (data, response) = try await URLSession.shared.data(for: request)
        try Self.validate(response: response, data: data)
    }

    func status() async throws -> BackendStatus {
        let (data, response) = try await URLSession.shared.data(from: baseURL.appendingPathComponent("status"))
        try Self.validate(response: response, data: data)
        return try decoder.decode(BackendStatus.self, from: data)
    }

    /// Cheap liveness probe. Returns true if the backend is reachable and returns 2xx,
    /// false otherwise (never throws — meant for frequent UI health pings).
    func isReachable() async -> Bool {
        var request = URLRequest(url: baseURL.appendingPathComponent("health"))
        request.timeoutInterval = 2.0
        do {
            let (_, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse else { return false }
            return (200..<300).contains(http.statusCode)
        } catch {
            return false
        }
    }

    func settings() async throws -> BackendSettings {
        let (data, response) = try await URLSession.shared.data(from: baseURL.appendingPathComponent("settings"))
        try Self.validate(response: response, data: data)
        return try decoder.decode(BackendSettings.self, from: data)
    }

    func setLiveSubmit(enabled: Bool) async throws -> BackendSettings {
        var request = URLRequest(url: baseURL.appendingPathComponent("settings/live_submit"))
        request.httpMethod = "PUT"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: ["enabled": enabled])
        let (data, response) = try await URLSession.shared.data(for: request)
        try Self.validate(response: response, data: data)
        return try decoder.decode(BackendSettings.self, from: data)
    }

    func setAutoSubmit(enabled: Bool) async throws -> BackendSettings {
        var request = URLRequest(url: baseURL.appendingPathComponent("settings/auto_submit"))
        request.httpMethod = "PUT"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: ["enabled": enabled])
        let (data, response) = try await URLSession.shared.data(for: request)
        try Self.validate(response: response, data: data)
        return try decoder.decode(BackendSettings.self, from: data)
    }

    func setClassifierAutoPass(enabled: Bool) async throws -> BackendSettings {
        var request = URLRequest(url: baseURL.appendingPathComponent("settings/classifier_auto_pass"))
        request.httpMethod = "PUT"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: ["enabled": enabled])
        let (data, response) = try await URLSession.shared.data(for: request)
        try Self.validate(response: response, data: data)
        return try decoder.decode(BackendSettings.self, from: data)
    }

    func setLiveMode(enabled: Bool) async throws -> BackendSettings {
        var request = URLRequest(url: baseURL.appendingPathComponent("settings/live_mode"))
        request.httpMethod = "PUT"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: ["enabled": enabled])
        let (data, response) = try await URLSession.shared.data(for: request)
        try Self.validate(response: response, data: data)
        return try decoder.decode(BackendSettings.self, from: data)
    }

    /// Ask the backend to read whatever the user just typed into the currently-focused field in the live
    /// Chrome window, and use that as the answer to the pending alarm.
    /// Returns the value that was read, or nil if nothing was focused.
    func readBrowserAnswer(question: String) async throws -> String? {
        var request = URLRequest(url: baseURL.appendingPathComponent("gap/read_browser"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: ["question": question])
        let (data, response) = try await URLSession.shared.data(for: request)
        try Self.validate(response: response, data: data)
        let payload = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        return payload?["value"] as? String
    }

    func focusBrowser() async throws -> String? {
        var request = URLRequest(url: baseURL.appendingPathComponent("browser/focus"))
        request.httpMethod = "POST"
        let (data, response) = try await URLSession.shared.data(for: request)
        try Self.validate(response: response, data: data)
        let payload = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        let ok = payload?["ok"] as? Bool ?? false
        if !ok {
            throw BackendClientError.invalidPayload("No automation browser window is available to focus yet")
        }
        return payload?["url"] as? String
    }

    func openCurrentPageInDefaultBrowser() async throws -> String? {
        var request = URLRequest(url: baseURL.appendingPathComponent("browser/open_external"))
        request.httpMethod = "POST"
        let (data, response) = try await URLSession.shared.data(for: request)
        try Self.validate(response: response, data: data)
        let payload = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        let ok = payload?["ok"] as? Bool ?? false
        if !ok {
            throw BackendClientError.invalidPayload("No current page is available to open in your default browser")
        }
        return payload?["url"] as? String
    }

    func applications(limit: Int = 100) async throws -> [ApplicationRow] {
        let url = Self.urlWithQuery(baseURL.appendingPathComponent("applications"), items: [URLQueryItem(name: "limit", value: "\(limit)")])
        let (data, response) = try await URLSession.shared.data(from: url)
        try Self.validate(response: response, data: data)
        return try decoder.decode([ApplicationRow].self, from: data)
    }

    func deleteApplication(jobURL: String) async throws {
        let url = Self.urlWithQuery(baseURL.appendingPathComponent("applications"), items: [URLQueryItem(name: "job_url", value: jobURL)])
        var request = URLRequest(url: url)
        request.httpMethod = "DELETE"
        let (data, response) = try await URLSession.shared.data(for: request)
        try Self.validate(response: response, data: data)
    }

    func deleteApplicationsByCompany(companies: [String]) async throws {
        var request = URLRequest(url: baseURL.appendingPathComponent("applications/by-company"))
        request.httpMethod = "DELETE"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: ["companies": companies])
        let (data, response) = try await URLSession.shared.data(for: request)
        try Self.validate(response: response, data: data)
    }

    func clearHistory() async throws {
        var request = URLRequest(url: baseURL.appendingPathComponent("history/clear"))
        request.httpMethod = "POST"
        let (data, response) = try await URLSession.shared.data(for: request)
        try Self.validate(response: response, data: data)
    }

    func runs(limit: Int = 50) async throws -> [RunRow] {
        let url = Self.urlWithQuery(baseURL.appendingPathComponent("runs"), items: [URLQueryItem(name: "limit", value: "\(limit)")])
        let (data, response) = try await URLSession.shared.data(from: url)
        try Self.validate(response: response, data: data)
        return try decoder.decode([RunRow].self, from: data)
    }

    func pendingActions() async throws -> PendingActionsPayload {
        let (data, response) = try await URLSession.shared.data(from: baseURL.appendingPathComponent("pending_actions"))
        try Self.validate(response: response, data: data)
        return try decoder.decode(PendingActionsPayload.self, from: data)
    }

    func siteLimits() async throws -> [SiteLimitRow] {
        let (data, response) = try await URLSession.shared.data(from: baseURL.appendingPathComponent("config/site_limits"))
        try Self.validate(response: response, data: data)
        return try decoder.decode([SiteLimitRow].self, from: data)
    }

    func setSiteLimit(domain: String, dailyLimit: Int?) async throws {
        var request = URLRequest(url: baseURL.appendingPathComponent("config/site_limits"))
        request.httpMethod = "PUT"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        var body: [String: Any] = ["domain": domain]
        body["daily_limit"] = dailyLimit.map { $0 } ?? NSNull()
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        let (data, response) = try await URLSession.shared.data(for: request)
        try Self.validate(response: response, data: data)
    }

    func logs(maxChars: Int = 120_000) async throws -> BackendLogs {
        let url = Self.urlWithQuery(baseURL.appendingPathComponent("logs"), items: [URLQueryItem(name: "max_chars", value: "\(maxChars)")])
        let (data, response) = try await URLSession.shared.data(from: url)
        try Self.validate(response: response, data: data)
        return try decoder.decode(BackendLogs.self, from: data)
    }

    /// Safely attach query items without crashing on exotic URL edge cases. If URLComponents cannot
    /// parse the base URL for any reason, we fall back to the raw endpoint which is still valid
    /// for the localhost backend.
    private static func urlWithQuery(_ base: URL, items: [URLQueryItem]) -> URL {
        guard var components = URLComponents(url: base, resolvingAgainstBaseURL: false) else {
            return base
        }
        components.queryItems = items
        return components.url ?? base
    }

    func clearLogs() async throws {
        var request = URLRequest(url: baseURL.appendingPathComponent("logs/clear"))
        request.httpMethod = "POST"
        let (data, response) = try await URLSession.shared.data(for: request)
        try Self.validate(response: response, data: data)
    }

    func fillGap(question: String, answer: String) async throws {
        var request = URLRequest(url: baseURL.appendingPathComponent("gap/fill"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: ["question": question, "answer": answer])
        let (data, response) = try await URLSession.shared.data(for: request)
        try Self.validate(response: response, data: data)
    }

    func skipField(question: String) async throws {
        var request = URLRequest(url: baseURL.appendingPathComponent("gap/skip"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: ["question": question])
        let (data, response) = try await URLSession.shared.data(for: request)
        try Self.validate(response: response, data: data)
    }

    func respondToApproval(token: String, approved: Bool, fieldAnswers: [ReviewField]) async throws {
        var request = URLRequest(url: baseURL.appendingPathComponent("approval/respond"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let answers = fieldAnswers.map { field in
            [
                "key": field.key,
                "label": field.label,
                "value": field.value,
                "required": field.required,
                "field_type": field.fieldType,
                "editable": field.editable
            ] as [String: Any]
        }
        request.httpBody = try JSONSerialization.data(
            withJSONObject: ["token": token, "approved": approved, "field_answers": answers]
        )
        let (data, response) = try await URLSession.shared.data(for: request)
        try Self.validate(response: response, data: data)
    }

    func respondToClassifierReview(token: String, passed: Bool) async throws {
        var request = URLRequest(url: baseURL.appendingPathComponent("classifier/respond"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: ["token": token, "passed": passed])
        let (data, response) = try await URLSession.shared.data(for: request)
        try Self.validate(response: response, data: data)
    }

    func respondToManualTakeover(token: String, action: String, buttonType: String? = nil, buttonText: String? = nil) async throws {
        var requestBody: [String: Any] = ["token": token, "action": action]
        if let buttonType = buttonType, let buttonText = buttonText, !buttonText.trimmingCharacters(in: .whitespaces).isEmpty {
            requestBody["registered_button"] = ["type": buttonType.lowercased(), "name": buttonText]
        }
        var request = URLRequest(url: baseURL.appendingPathComponent("manual/respond"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: requestBody)
        let (data, response) = try await URLSession.shared.data(for: request)
        try Self.validate(response: response, data: data)
    }

    func streamEvents() -> AsyncThrowingStream<SSEEvent, Error> {
        let url = baseURL.appendingPathComponent("stream")
        return AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    let (bytes, _) = try await URLSession.shared.bytes(from: url)
                    var eventName = "message"
                    var dataLines: [String] = []
                    for try await line in bytes.lines {
                        if line.hasPrefix("event:") {
                            eventName = line.dropFirst(6).trimmingCharacters(in: .whitespaces)
                        } else if line.hasPrefix("data:") {
                            dataLines.append(line.dropFirst(5).trimmingCharacters(in: .whitespaces))
                        } else if line.isEmpty {
                            if !dataLines.isEmpty {
                                let data = dataLines.joined(separator: "\n").data(using: .utf8) ?? Data()
                                let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
                                continuation.yield(SSEEvent(name: eventName, data: object))
                            }
                            eventName = "message"
                            dataLines = []
                        }
                    }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    private static func validate(response: URLResponse, data: Data) throws {
        guard let http = response as? HTTPURLResponse else { return }
        guard (200..<300).contains(http.statusCode) else {
            let body = String(data: data, encoding: .utf8) ?? "<empty response>"
            throw BackendClientError.httpStatus(http.statusCode, body)
        }
    }
}

struct SSEEvent {
    let name: String
    let data: [String: Any]
}

struct SiteLimitRow: Codable, Identifiable {
    var id: String { domain }
    let domain: String
    let dailyLimit: Int?
    let appliedToday: Int

    enum CodingKeys: String, CodingKey {
        case domain
        case dailyLimit = "daily_limit"
        case appliedToday = "applied_today"
    }
}

struct PendingActionsPayload: Decodable {
    let classifierReviews: [PendingClassifierReview]
    let approvals: [PendingApproval]
    let manualTakeovers: [PendingManualTakeover]
    let alarms: [PendingAlarmState]?

    enum CodingKeys: String, CodingKey {
        case classifierReviews = "classifier_reviews"
        case approvals
        case manualTakeovers = "manual_takeovers"
        case alarms
    }
}

struct PendingAlarmState: Decodable {
    let token: String
    let question: String
    let fieldType: String?
    let options: [String]?

    enum CodingKeys: String, CodingKey {
        case token
        case question
        case fieldType = "field_type"
        case options
    }
}
