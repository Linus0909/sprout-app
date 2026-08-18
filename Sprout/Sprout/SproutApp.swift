import SwiftUI

@main
struct SproutApp: App {
    var body: some Scene {
        WindowGroup {
            ContentView()
                .ignoresSafeArea()
                .preferredColorScheme(nil) // follows system Light/Dark, matching the web app's Auto theme
        }
    }
}
