/**
 * Check if a user exists with the given email
 *
 * @param {string} email - The email to check
 * @returns {Promise} - Promise that resolves with the API response
 */
export const checkUserExists = async (email) => {
  try {
    console.log("Checking if user exists:", email);

    // TODO: remove this and replace with actual API call
    await new Promise((resolve) => setTimeout(resolve, 500));

    if (email === "exists@example.com" || email === "google.user@example.com") {
      return {
        ok: true,
        exists: true,
      };
    }

    return {
      ok: true,
      exists: false,
    };
  } catch (error) {
    console.error("Error checking if user exists:", error);
    return {
      ok: false,
      error: "An unexpected error occurred. Please try again.",
    };
  }
};

/**
 * Authenticate a user with email and password
 *
 * @param {Object} credentials - User credentials (email, password)
 * @returns {Promise} - Promise that resolves with the API response
 */
export const authenticateUser = async (credentials) => {
  const { email, password } = credentials;

  try {
    console.log("Authenticating user:", email);

    // TODO: remove this and replace with actual API call
    await new Promise((resolve) => setTimeout(resolve, 800));

    if (email === "error@example.com") {
      return {
        ok: false,
        error: "Invalid email or password",
      };
    } else if (email === "unverified@example.com") {
      return {
        ok: false,
        error: "Please verify your email before logging in",
      };
    }

    // Generate a mock session token, TODO; remove this and replace with actual token generation
    const sessionToken = generateSessionToken();

    return {
      ok: true,
      user: {
        name: email.split("@")[0],
        email,
      },
      sessionToken,
    };
  } catch (error) {
    console.error("Authentication error:", error);
    return {
      ok: false,
      error: "An unexpected error occurred. Please try again.",
    };
  }
};

/**
 * Authenticate a user with OAuth
 *
 * @param {Object} oauthData - OAuth data (provider, token)
 * @returns {Promise} - Promise that resolves with the API response
 */
export const authenticateWithOAuth = async (oauthData) => {
  const { provider, token } = oauthData;

  try {
    console.log(`Authenticating user with ${provider} OAuth`);

    // TODO: remove this and replace with actual API call
    await new Promise((resolve) => setTimeout(resolve, 800));

    // In the filan implementatino, this would validate the token with the provider
    // and retrieve the user information from the database
    // For now, we'll simulate a successful authentication
    // TODO: remove this and replace with actual token validation

    // For now, we'll decode the token and use the information from it
    const { jwtDecode } = await import("jwt-decode");
    const decodedToken = jwtDecode(token);

    // Generate a mock session token
    const sessionToken = generateSessionToken();

    return {
      ok: true,
      user: {
        name: decodedToken.name || "OAuth User",
        email: decodedToken.email || "oauth.user@example.com",
      },
      sessionToken,
    };
  } catch (error) {
    console.error(`${provider} OAuth authentication error:`, error);
    return {
      ok: false,
      error: "An unexpected error occurred. Please try again.",
    };
  }
};

/**
 * Generate a session token
 *
 * @returns {string} - A session token
 */
const generateSessionToken = () => {
  return (
    Math.random().toString(36).substring(2, 15) +
    Math.random().toString(36).substring(2, 15)
  );
};

/**
 * Get Grafana visualization data
 *
 * @returns {Promise} - Promise that resolves with visualization data
 */
export const getVisualizationData = async () => {
  try {
    // In the end website, this would fetch data from a Grafana API
    // For now, we'll return mock data
    // TODO: remove this and replace with actual API call

    await new Promise((resolve) => setTimeout(resolve, 1000));

    return {
      ok: true,
      data: {
        systemMetrics: generateRandomData(30),
        performanceAnalytics: generateRandomData(30),
        userActivity: generateRandomData(30),
        resourceUtilization: generateRandomData(30),
      },
    };
  } catch (error) {
    console.error("Error fetching visualization data:", error);
    return {
      ok: false,
      error: "Failed to load visualization data",
    };
  }
};

/**
 * Generate random data for visualizations
 *
 * @param {number} points - Number of data points to generate
 * @returns {Array} - Array of data points
 */
const generateRandomData = (points) => {
  return Array.from({ length: points }, (_, i) => ({
    timestamp: Date.now() - (points - i) * 3600000, // hourly data points
    value: Math.floor(Math.random() * 100),
  }));
};
