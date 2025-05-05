import CryptoJS from "crypto-js";

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
  try {
    // Prepare the request body
    const requestBody = {
      name,
      email,
      password,
    };

    // If we have a Google credential, include it in the request
    if (googleCredential) {
      requestBody.token = googleCredential;
      requestBody.provider = "google";
    }

    const response = await fetch(
      `${process.env["REACT_APP_BACKEND_HOST"]}:${process.env["REACT_APP_BACKEND_PORT"]}/api/user/create/`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify(requestBody),
      },
    );
    const responseBody = await response.json();
    if (!response.ok) {
      return {
        ok: false,
        error:
          response["status"] +
          ": " +
          response["statusText"] +
          " => " +
          responseBody["message"],
      };
    } else {
      return {
        ok: true,
        message: responseBody["message"],
        userToken: responseBody["user_token"],
        sessionToken: responseBody["session_token"],
      };
    }
  } catch (error) {
    console.error("Error creating user:", error);
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

    const response = await fetch(
      `${process.env["REACT_APP_BACKEND_HOST"]}:${process.env["REACT_APP_BACKEND_PORT"]}/api/user/authenticate/`,
      {
        method: "POST", // or "GET", "PUT", etc., depending on your API
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ email: email, password: password }),
      },
    );
    const responseBody = await response.json();

    if (!response.ok) {
      return {
        ok: false,
        error: responseBody["message"],
      };
    } else {
      return {
        ok: true,
        message: responseBody["message"],
        user: responseBody["user"],
        userToken: responseBody["user_token"],
        sessionToken: responseBody["session_token"],
      };
    }
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
    const response = await fetch(
      `${process.env["REACT_APP_BACKEND_HOST"]}:${process.env["REACT_APP_BACKEND_PORT"]}/api/user/authenticate/`,
      {
        method: "POST", // or "GET", "PUT", etc., depending on your API
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ token: token, provider: provider }),
      },
    );
    const responseBody = await response.json();

    if (!response.ok) {
      return {
        ok: false,
        error: responseBody["message"],
      };
    } else {
      return {
        ok: true,
        message: responseBody["message"],
        user: response["user"],
        userToken: response["user_token"],
        sessionToken: response["session_token"],
      };
    }
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
export const generateSessionToken = () => {
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
