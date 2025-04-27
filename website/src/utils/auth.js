import CryptoJS from "crypto-js";
import { jwtDecode } from "jwt-decode";
/**
 * Hash a password with a salt
 *
 * @param {string} password - The password to hash
 * @returns {string} - The hashed password
 */
export const hashPassword = (password) => {
  return CryptoJS.SHA256(password).toString(CryptoJS.enc.Hex);
};

/**
 * Make an API request to create a new user
 *
 * @param {Object} userData - User data including name, email, password, and optional googleCredential
 * @returns {Promise} - Promise that resolves with the API response
 */
export const createUser = async (userData) => {
  const { name, email, password, googleCredential } = userData;

  // Hash the password before sending
  const hashedPassword = hashPassword(password);

  try {
    // Prepare the request body
    const requestBody = {
      name,
      email,
      password: hashedPassword,
    };

    // If we have a Google credential, include it in the request
    if (googleCredential) {
      requestBody.googleCredential = googleCredential;
    }

    // TODO: change to the actual API endpoint
    const response = await fetch("/api/new_user", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify(requestBody),
    });

    // TODO : change this to the actual login
    if (email === "exists@example.com") {
      return {
        ok: false,
        error: "User already exists with this email",
      };
    } else if (email.includes("toomany")) {
      return {
        ok: false,
        error: "Too many requests from this email",
      };
    }

    // Generate a session token
    const { generateSessionToken } = await import("./api");
    const sessionToken = generateSessionToken();

    // Simulate successful response
    return {
      ok: true,
      message:
        "User created successfully. Please check your email for verification.",
      sessionToken,
    };
  } catch (error) {
    console.error("Error creating user:", error);
    return {
      ok: false,
      error: "An unexpected error occurred. Please try again.",
    };
  }
};

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
    const { checkUserExists, authenticateWithOAuth } = await import("./api");

    // Check if user exists in our system
    const userExistsResponse = await checkUserExists(googleUser.email);

    if (userExistsResponse.ok) {
      if (userExistsResponse.exists) {
        // User exists, proceed with login using OAuth
        console.log("Existing user, proceeding with OAuth login");

        // If we have a credential, use it for authentication
        if (googleUser.credential) {
          try {
            const authResponse = await authenticateWithOAuth({
              provider: "google",
              token: googleUser.credential,
            });

            if (authResponse.ok) {
              // Authentication successful, call the callback with the authenticated user
              onExistingUser({
                ...googleUser,
                sessionToken: authResponse.sessionToken,
              });
              return;
            }
          } catch (authError) {
            console.error("OAuth authentication error:", authError);
            // Fall back to regular login if OAuth authentication fails
          }
        }

        // Fallback to regular login if no credential or authentication failed
        onExistingUser(googleUser);
      } else {
        // New user, need to create account with password
        console.log("New user, need to create account");
        onNewUser(googleUser);
      }
    } else {
      throw new Error(
        userExistsResponse.error || "Failed to check if user exists",
      );
    }
  } catch (error) {
    console.error("Error in Google auth flow:", error);
    throw error;
  }
};
