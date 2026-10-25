import AppIntents
import Foundation
import Observation

/// "Hey Siri, scan with Lea": opens the app straight into a new scan with what's wrong filled in. Siri asks for it
/// ("What's wrong with it?") and the home screen starts the scan with the answer as the person's description.
struct ScanWithLeaIntent: AppIntent {
    static let title: LocalizedStringResource = "Scan with Lea"
    static let description = IntentDescription("Start a new scan, with what's wrong already filled in.")
    static let openAppWhenRun = true

    @Parameter(title: "What's wrong", requestValueDialog: IntentDialog("What's wrong with it?"))
    var story: String

    @MainActor
    func perform() async throws -> some IntentResult {
        IntentInbox.shared.requestScan(story: story)
        return .result()
    }
}

/// "Hey Siri, hey lea" (or hold the side button and say "hey lea"): opens the app into a conversation with Lea,
/// already listening hands-free, and she asks what's up. From the lock screen, iOS asks to unlock first.
struct TalkToLeaIntent: AppIntent {
    static let title: LocalizedStringResource = "Talk to Lea"
    static let description = IntentDescription("Opens Lea listening, so you can say what's wrong.")
    static let openAppWhenRun = true

    @MainActor
    func perform() async throws -> some IntentResult {
        IntentInbox.shared.requestTalk()
        return .result()
    }
}

struct LeaShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(intent: ScanWithLeaIntent(),
                    phrases: ["Scan with \(.applicationName)", "Start a scan with \(.applicationName)",
                              "Check my device with \(.applicationName)", "Check something with \(.applicationName)",
                              "Ask \(.applicationName) what's wrong", "Open a scan in \(.applicationName)"],
                    shortTitle: "Scan with Lea", systemImageName: "viewfinder")
        AppShortcut(intent: TalkToLeaIntent(),
                    phrases: ["Hey \(.applicationName)", "Talk to \(.applicationName)", "Open \(.applicationName) and listen"],
                    shortTitle: "Talk to Lea", systemImageName: "waveform")
    }
}

/// Where the intent leaves its request until the home screen picks it up (the app may still be launching).
@MainActor
@Observable
final class IntentInbox {
    static let shared = IntentInbox()
    private(set) var pending: String?
    /// Goes up on every request, so the home screen reacts even to the same words twice.
    private(set) var requests = 0

    /// Goes up on every "Hey lea" from Siri (the home screen opens the conversation, listening).
    private(set) var talks = 0
    /// Lea greets once when that conversation opens.
    private var greet = false

    func requestTalk() {
        greet = true
        talks += 1
    }

    /// True once per "Hey lea".
    func takeGreeting() -> Bool {
        defer { greet = false }
        return greet
    }

    func requestScan(story: String) {
        pending = story.trimmingCharacters(in: .whitespacesAndNewlines)
        requests += 1
    }

    /// The waiting story (nil when there is no request); taking it clears it.
    func take() -> String? {
        defer { pending = nil }
        return pending
    }
}
