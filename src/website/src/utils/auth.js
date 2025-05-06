import CryptoJS from "crypto-js";
import { jwtDecode } from "jwt-decode";
/**
 * Initialize Google OAuth and process the credential response
 *
 * @param {Object} credentialResponse - The response from Google OAuth
 * @returns {Promise<Object>} - Promise that resolves with the user data
 */
export const initGoogleOAuth = async (credentialResponse) => {
  try {
    console.log("Processing Google credential:", credentialResponse);

    // decode the JWT token to get user info
    const decodedToken = jwtDecode(credentialResponse.credential);
    console.log("Decoded Google token:", decodedToken);

    // Extract user information from the decoded token
    const googleUser = {
      name: decodedToken.name || "Google User",
      email: decodedToken.email || "google.user@example.com",
      googleId: decodedToken.sub || "mock-google-id",
      // Store the JWT credential for authentication
      credential: credentialResponse.credential,
    };

    return googleUser;
  } catch (error) {
    console.error("Error processing Google credential:", error);
    throw new Error("Failed to process Google authentication");
  }
};

/**
 * Handle Google authentication flow
 *
 * @param {Object} googleUser - User data from Google
 * @param {Function} onExistingUser - Callback for existing users
 * @param {Function} onNewUser - Callback for new users
 * @returns {Promise<void>}
 */
export const handleGoogleAuth = async (
  googleUser,
  onExistingUser,
  onNewUser,
) => {
  try {
    const { authenticateWithOAuth } = await import("./api");

    // Check if user exists in our system
    const authResponse = await authenticateWithOAuth({
      provider: "google",
      token: googleUser.credential,
    });

    if (authResponse.ok) {
      // Authentication successful, call the callback with the authenticated user
      console.log("Existing user, proceeding with OAuth login");
      onExistingUser({
        ...googleUser,
        sessionToken: authResponse.sessionToken,
      });
    } else {
      // New user, need to create account with password
      console.log("New user, need to create account");
      onNewUser(googleUser);
    }
  } catch (error) {
    console.error("Error in Google auth flow:", error);
    throw error;
  }
};
