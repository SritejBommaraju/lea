import SwiftUI

@main
struct RepairOSApp: App {
    @StateObject private var app = AppState()

    init() {
        #if DEBUG
        ScreensHarness.prepare()   // `-screens <name>`: a fixed state for screenshots (ios/RepairOSUITests)
        #endif
    }

    var body: some Scene {
        WindowGroup {
            root
                .environmentObject(app)
                .overlay(alignment: .top) { UsageBanner() }   // a paid service on the Mac paused (spend_alert)
                .task { UsageAlerts.shared.start(app) }
                .task {
                    await app.launch()
                    LeaVoice.shared.attach(app)
                    LeaShortcuts.updateAppShortcutParameters()   // Siri: "Scan with lea" (Voice/ScanIntent)
                }
        }
    }

    @ViewBuilder private var root: some View {
        #if DEBUG
        if let screen = ScreensHarness.screen { ScreensHarness.root(screen, app: app) }
        else if let screen = UIGallery.screen { UIGallery.root(screen, app: app) }
        else { RootView() }
        #else
        RootView()
        #endif
    }
}
