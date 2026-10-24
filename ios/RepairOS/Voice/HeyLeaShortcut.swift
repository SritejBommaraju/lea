import AppIntents
import Foundation

/// "Hey Siri, hey lea" from anywhere: Siri opens the app, and Lea greets you and listens for one turn.
/// App Shortcuts register with the system when the app is installed; nothing to set up in the Shortcuts app.
/// (Only Siri's own wake word works outside the app; inside it, hands-free or the "Hey Leah" model take over.)
struct TalkToLeaIntent: AppIntent {
    static let title: LocalizedStringResource = "Talk to Lea"
    static let description = IntentDescription("Opens Lea and starts listening, so you can say what's wrong.")
    static let openAppWhenRun = true

    @MainActor
    func perform() async throws -> some IntentResult {
        VoiceLaunch.request()
        return .result()
    }
}

struct LeaShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(intent: TalkToLeaIntent(),
                    phrases: ["Hey \(.applicationName)", "Talk to \(.applicationName)", "Ask \(.applicationName) what's wrong",
                              "Open \(.applicationName) and listen"],
                    shortTitle: "Talk to Lea", systemImageName: "waveform")
    }
}

/// A listen request from Siri, kept until the voice bar is ready to take it (the app may still be launching).
@MainActor
enum VoiceLaunch {
    static let requested = Notification.Name("lea.voice.listenRequested")
    private(set) static var pending = false

    static func request() {
        pending = true
        NotificationCenter.default.post(name: requested, object: nil)
    }

    /// True once per request.
    static func take() -> Bool {
        defer { pending = false }
        return pending
    }
}
