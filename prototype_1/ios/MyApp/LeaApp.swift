import SwiftUI

@main
struct LeaApp: App {
    @StateObject private var store = CaptureStore()
    @StateObject private var sync = SessionSync()
    @StateObject private var conversation = VoiceCoordinator()
    @StateObject private var actions = LeaActions()

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(store)
                .environmentObject(sync)
                .environmentObject(conversation)
                .environmentObject(actions)
                .onAppear {
                    sync.attach(store)
                    conversation.attach(sync, actions: actions)
                    conversation.wakeDetector = SoundWakeDetector()
                }
                .preferredColorScheme(.dark)
        }
    }
}
