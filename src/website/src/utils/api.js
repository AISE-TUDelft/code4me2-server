import CryptoJS from "crypto-js";

// Build backend base URL with optional port. Default host points to production API.
const BACKEND_HOST = process.env.REACT_APP_BACKEND_HOST || "https://api.code4me.me";
const BACKEND_PORT = Number(process.env.REACT_APP_BACKEND_PORT || 0);
export const BASE_URL = (() => {
  const host = BACKEND_HOST.replace(/\/$/, "");
  return BACKEND_PORT > 0 ? `${host}:${BACKEND_PORT}` : host;
})();

// Intercept fetch calls globally to normalize backend URLs (host/port + trailing slash)
const __origFetch = typeof window !== "undefined" ? window.fetch : undefined;
if (__origFetch) {
  // Wrap only once per module load
  const PREFIX = `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}`;
  window.fetch = (input, init) => {
    try {
      if (typeof input === "string") {
        let urlStr = input;
        // Normalize host + optional port to computed BASE_URL
        if (PREFIX && urlStr.startsWith(PREFIX)) {
          urlStr = BASE_URL + urlStr.substring(PREFIX.length);
        }
        // If URL appears absolute, normalize trailing slash for /api/* paths
        try {
          const u = new URL(urlStr);
          if (u.pathname.startsWith("/api/") && u.pathname !== "/api" && u.pathname.endsWith("/")) {
            // Strip only trailing slashes to match FastAPI canonical paths and avoid 307 redirects
            u.pathname = u.pathname.replace(/\/+$/, "");
            urlStr = u.toString();
          }
        } catch (_) {
          // not an absolute URL (e.g., relative path). Leave as-is.
        }
        input = urlStr;
      }
    } catch (e) {
      // no-op
    }
    return __origFetch(input, init);
  };
}

// Safely parse a fetch Response into JSON if possible; otherwise return a fallback
const parseResponseSafe = async (response) => {
  try {
    const clone = response.clone();
    const ct = clone.headers.get("content-type") || "";
    if (ct.toLowerCase().includes("application/json")) {
      return await clone.json();
    }
  } catch (_) {
    // ignore
  }
  try {
    const text = await response.clone().text();
    if (text && text.trim()) {
      return { message: text };
    }
  } catch (_) {
    // ignore
  }
  return null;
};

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
      config_id: 1, // Default config ID - you may want to make this configurable
    };

    // If we have a Google credential, include it in the request
    if (googleCredential) {
      requestBody.token = googleCredential;
      requestBody.provider = "google";
    }

    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/user/create/`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        credentials: "include", // Include cookies in the request
        body: JSON.stringify(requestBody),
      },
    );
    const responseBody = await response.json();
    if (!response.ok) {
      return {
        ok: false,
        error: responseBody["message"] || `${response.status}: ${response.statusText}`,
      };
    } else {
      return {
        ok: true,
        message: responseBody["message"],
        user_id: responseBody["user_id"],
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
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/user/authenticate/`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        credentials: "include", // Include cookies for auth token
        body: JSON.stringify({ email: email, password: password }),
      },
    );
    const responseBody = await parseResponseSafe(response);

    if (!response.ok) {
      const errMsg = (responseBody && (responseBody["message"] || responseBody["detail"])) || `${response.status}: ${response.statusText}`;
      return {
        ok: false,
        error: errMsg,
      };
    } else {
      if (!responseBody || typeof responseBody !== "object") {
        return { ok: false, error: "Empty or invalid server response." };
      }
      return {
        ok: true,
        message: responseBody["message"],
        user: responseBody["user"],
        config: responseBody["config"],
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
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/user/authenticate/`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        credentials: "include", // Include cookies for auth token
        body: JSON.stringify({ token: token, provider: provider }),
      },
    );
    const responseBody = await parseResponseSafe(response);

    if (!response.ok) {
      const errMsg = (responseBody && (responseBody["message"] || responseBody["detail"])) || `${response.status}: ${response.statusText}`;
      return {
        ok: false,
        error: errMsg,
      };
    } else {
      if (!responseBody || typeof responseBody !== "object") {
        return { ok: false, error: "Empty or invalid server response." };
      }
      return {
        ok: true,
        message: responseBody["message"],
        user: responseBody["user"],
        config: responseBody["config"],
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
 * Get current user information from auth token
 *
 * @returns {Promise} - Promise that resolves with user data
 */
export const getCurrentUser = async () => {
  try {
    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/user/get/`,
      {
        method: "GET",
        headers: {
          "Content-Type": "application/json",
        },
        credentials: "include", // Include auth token cookie
      },
    );
    const responseBody = await response.json();

    if (!response.ok) {
      return {
        ok: false,
        error: responseBody["message"] || `${response.status}: ${response.statusText}`,
      };
    } else {
      return {
        ok: true,
        user: responseBody["user"],
        config: responseBody["config"],
      };
    }
  } catch (error) {
    console.error("Error getting current user:", error);
    return {
      ok: false,
      error: "An unexpected error occurred. Please try again.",
    };
  }
};

/**
 * Check if current user is verified
 *
 * @returns {Promise} - Promise that resolves with verification status
 */
export const checkVerificationStatus = async () => {
  try {
    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/user/verify/check/`,
      {
        method: "GET",
        headers: {
          "Content-Type": "application/json",
        },
        credentials: "include", // Include auth token cookie
      },
    );
    const responseBody = await response.json();

    if (!response.ok) {
      return {
        ok: false,
        error: responseBody["message"] || `${response.status}: ${response.statusText}`,
      };
    } else {
      return {
        ok: true,
        verified: responseBody["user_is_verified"],
      };
    }
  } catch (error) {
    console.error("Error checking verification status:", error);
    return {
      ok: false,
      error: "An unexpected error occurred. Please try again.",
    };
  }
};

/**
 * Resend verification email
 *
 * @returns {Promise} - Promise that resolves with API response
 */
export const resendVerificationEmail = async () => {
  try {
    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/user/verify/resend/`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        credentials: "include", // Include auth token cookie
      },
    );
    const responseBody = await response.json();

    if (!response.ok) {
      return {
        ok: false,
        error: responseBody["message"] || `${response.status}: ${response.statusText}`,
      };
    } else {
      return {
        ok: true,
        message: responseBody["message"] || "Verification email request queued",
      };
    }
  } catch (error) {
    console.error("Error resending verification email:", error);
    return {
      ok: false,
      error: "An unexpected error occurred. Please try again.",
    };
  }
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

// ===== ANALYTICS API FUNCTIONS =====

/**
 * Get dashboard overview data
 *
 * @param {string} timeWindow - Time window (1d, 7d, 30d)
 * @returns {Promise} - Promise that resolves with overview data
 */
export const getDashboardOverview = async (timeWindow = "7d") => {
  try {
    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/analytics/overview/dashboard?time_window=${timeWindow}`,
      {
        method: "GET",
        headers: {
          "Content-Type": "application/json",
        },
        credentials: "include", // Include auth token cookie
      },
    );
    const responseBody = await response.json();

    if (!response.ok) {
      return {
        ok: false,
        error: responseBody["detail"] || `${response.status}: ${response.statusText}`,
      };
    } else {
      return {
        ok: true,
        data: responseBody,
      };
    }
  } catch (error) {
    console.error("Error fetching dashboard overview:", error);
    return {
      ok: false,
      error: "An unexpected error occurred. Please try again.",
    };
  }
};

/**
 * Get activity timeline data
 *
 * @param {string} timeWindow - Time window (1h, 6h, 24h, 7d)
 * @param {string} granularity - Data granularity (5m, 15m, 1h, 1d)
 * @returns {Promise} - Promise that resolves with timeline data
 */
export const getActivityTimeline = async (timeWindow = "24h", granularity = "1h") => {
  try {
    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/analytics/overview/activity-timeline?time_window=${timeWindow}&granularity=${granularity}`,
      {
        method: "GET",
        headers: {
          "Content-Type": "application/json",
        },
        credentials: "include",
      },
    );
    const responseBody = await response.json();

    if (!response.ok) {
      return {
        ok: false,
        error: responseBody["detail"] || `${response.status}: ${response.statusText}`,
      };
    } else {
      return {
        ok: true,
        data: responseBody,
      };
    }
  } catch (error) {
    console.error("Error fetching activity timeline:", error);
    return {
      ok: false,
      error: "An unexpected error occurred. Please try again.",
    };
  }
};

/**
 * Get queries over time data
 *
 * @param {Object} params - Query parameters
 * @returns {Promise} - Promise that resolves with usage data
 */
export const getQueriesOverTime = async (params = {}) => {
  try {
    const queryParams = new URLSearchParams({
      granularity: params.granularity || "1h",
      ...params
    }).toString();

    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/analytics/usage/queries-over-time?${queryParams}`,
      {
        method: "GET",
        headers: {
          "Content-Type": "application/json",
        },
        credentials: "include",
      },
    );
    const responseBody = await response.json();

    if (!response.ok) {
      return {
        ok: false,
        error: responseBody["detail"] || `${response.status}: ${response.statusText}`,
      };
    } else {
      return {
        ok: true,
        data: responseBody,
      };
    }
  } catch (error) {
    console.error("Error fetching queries over time:", error);
    return {
      ok: false,
      error: "An unexpected error occurred. Please try again.",
    };
  }
};

/**
 * Get acceptance rates data
 *
 * @param {Object} params - Query parameters
 * @returns {Promise} - Promise that resolves with acceptance rate data
 */
export const getAcceptanceRates = async (params = {}) => {
  try {
    const queryParams = new URLSearchParams({
      group_by: params.group_by || "model",
      ...params
    }).toString();

    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/analytics/usage/acceptance-rates?${queryParams}`,
      {
        method: "GET",
        headers: {
          "Content-Type": "application/json",
        },
        credentials: "include",
      },
    );
    const responseBody = await response.json();

    if (!response.ok) {
      return {
        ok: false,
        error: responseBody["detail"] || `${response.status}: ${response.statusText}`,
      };
    } else {
      return {
        ok: true,
        data: responseBody,
      };
    }
  } catch (error) {
    console.error("Error fetching acceptance rates:", error);
    return {
      ok: false,
      error: "An unexpected error occurred. Please try again.",
    };
  }
};

/**
 * Get model comparison data
 *
 * @param {Object} params - Query parameters
 * @returns {Promise} - Promise that resolves with model comparison data
 */
export const getModelComparison = async (params = {}) => {
  try {
    const queryParams = new URLSearchParams(params).toString();

    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/analytics/models/comparison?${queryParams}`,
      {
        method: "GET",
        headers: {
          "Content-Type": "application/json",
        },
        credentials: "include",
      },
    );
    const responseBody = await response.json();

    if (!response.ok) {
      return {
        ok: false,
        error: responseBody["detail"] || `${response.status}: ${response.statusText}`,
      };
    } else {
      return {
        ok: true,
        data: responseBody,
      };
    }
  } catch (error) {
    console.error("Error fetching model comparison:", error);
    return {
      ok: false,
      error: "An unexpected error occurred. Please try again.",
    };
  }
};

const getAgentAnalytics = async (path, fallbackError) => {
  try {
    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/analytics/agents/${path}`,
      {
        method: "GET",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
      },
    );
    const body = await response.json();
    if (!response.ok) {
      return { ok: false, error: body["detail"] || `${response.status}: ${response.statusText}` };
    }
    return { ok: true, data: body };
  } catch (error) {
    console.error("Error fetching agent analytics:", error);
    return { ok: false, error: fallbackError };
  }
};

export const getAgentOverview = (params = {}) =>
  getAgentAnalytics(`overview?${new URLSearchParams(params)}`, "An unexpected error occurred. Please try again.");

export const getAgentRunDetail = (taskId) =>
  getAgentAnalytics(`runs/${encodeURIComponent(taskId)}`, "Failed to load agent run detail");

/**
 * Get user engagement data
 *
 * @param {string} timeWindow - Time window (7d, 30d, 90d)
 * @returns {Promise} - Promise that resolves with engagement data
 */
export const getUserEngagement = async (timeWindow = "30d") => {
  try {
    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/analytics/overview/user-engagement?time_window=${timeWindow}`,
      {
        method: "GET",
        headers: {
          "Content-Type": "application/json",
        },
        credentials: "include",
      },
    );
    const responseBody = await response.json();

    if (!response.ok) {
      return {
        ok: false,
        error: responseBody["detail"] || `${response.status}: ${response.statusText}`,
      };
    } else {
      return {
        ok: true,
        data: responseBody,
      };
    }
  } catch (error) {
    console.error("Error fetching user engagement:", error);
    return {
      ok: false,
      error: "An unexpected error occurred. Please try again.",
    };
  }
};

// ===== ADMIN-ONLY STUDY MANAGEMENT FUNCTIONS =====

/**
 * Get list of studies
 *
 * @param {boolean} includeInactive - Include inactive studies
 * @returns {Promise} - Promise that resolves with studies list
 */
export const getStudies = async (includeInactive = false) => {
  try {
    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/analytics/studies/list?include_inactive=${includeInactive}`,
      {
        method: "GET",
        headers: {
          "Content-Type": "application/json",
        },
        credentials: "include",
      },
    );
    const responseBody = await response.json();

    if (!response.ok) {
      return {
        ok: false,
        error: responseBody["detail"] || `${response.status}: ${response.statusText}`,
      };
    } else {
      return {
        ok: true,
        data: responseBody,
      };
    }
  } catch (error) {
    console.error("Error fetching studies:", error);
    return {
      ok: false,
      error: "An unexpected error occurred. Please try again.",
    };
  }
};

/**
 * Create a new study
 *
 * @param {Object} studyData - Study data
 * @returns {Promise} - Promise that resolves with creation result
 */
export const createStudy = async (studyData) => {
  try {
    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/analytics/studies/create`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        credentials: "include",
        body: JSON.stringify(studyData),
      },
    );
    const responseBody = await response.json();

    if (!response.ok) {
      return {
        ok: false,
        error: responseBody["detail"] || `${response.status}: ${response.statusText}`,
      };
    } else {
      return {
        ok: true,
        data: responseBody,
      };
    }
  } catch (error) {
    console.error("Error creating study:", error);
    return {
      ok: false,
      error: "An unexpected error occurred. Please try again.",
    };
  }
};

/**
 * Activate a study
 *
 * @param {string} studyId - Study ID
 * @returns {Promise} - Promise that resolves with activation result
 */
export const activateStudy = async (studyId) => {
  try {
    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/analytics/studies/${studyId}/activate`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        credentials: "include",
      },
    );
    const responseBody = await response.json();

    if (!response.ok) {
      return {
        ok: false,
        error: responseBody["detail"] || `${response.status}: ${response.statusText}`,
      };
    } else {
      return {
        ok: true,
        data: responseBody,
      };
    }
  } catch (error) {
    console.error("Error activating study:", error);
    return {
      ok: false,
      error: "An unexpected error occurred. Please try again.",
    };
  }
};

/**
 * Get study evaluation results
 *
 * @param {string} studyId - Study ID
 * @returns {Promise} - Promise that resolves with evaluation data
 */
export const getStudyEvaluation = async (studyId) => {
  try {
    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/analytics/studies/${studyId}/evaluation`,
      {
        method: "GET",
        headers: {
          "Content-Type": "application/json",
        },
        credentials: "include",
      },
    );
    const responseBody = await response.json();

    if (!response.ok) {
      return {
        ok: false,
        error: responseBody["detail"] || `${response.status}: ${response.statusText}`,
      };
    } else {
      return {
        ok: true,
        data: responseBody,
      };
    }
  } catch (error) {
    console.error("Error fetching study evaluation:", error);
    return {
      ok: false,
      error: "An unexpected error occurred. Please try again.",
    };
  }
};

/**
 * Get study details (assignments and metadata)
 *
 * @param {string} studyId - Study ID
 * @returns {Promise}
 */
export const getStudyDetails = async (studyId) => {
  try {
    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/analytics/studies/${studyId}/details`,
      {
        method: "GET",
        headers: {
          "Content-Type": "application/json",
        },
        credentials: "include",
      },
    );
    const responseBody = await response.json();

    if (!response.ok) {
      return {
        ok: false,
        error: responseBody["detail"] || `${response.status}: ${response.statusText}`,
      };
    } else {
      return {
        ok: true,
        data: responseBody,
      };
    }
  } catch (error) {
    console.error("Error fetching study details:", error);
    return {
      ok: false,
      error: "An unexpected error occurred. Please try again.",
    };
  }
};

/**
 * Deactivate a study
 *
 * @param {string} studyId - Study ID
 * @returns {Promise}
 */
export const deactivateStudy = async (studyId) => {
  try {
    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/analytics/studies/${studyId}/deactivate`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        credentials: "include",
      },
    );
    const responseBody = await response.json();

    if (!response.ok) {
      return {
        ok: false,
        error: responseBody["detail"] || `${response.status}: ${response.statusText}`,
      };
    } else {
      return {
        ok: true,
        data: responseBody,
      };
    }
  } catch (error) {
    console.error("Error deactivating study:", error);
    return {
      ok: false,
      error: "An unexpected error occurred. Please try again.",
    };
  }
};

/**
 * Logout the current user by deactivating their session
 *
 * @returns {Promise} - Promise that resolves with logout result
 */
export const logoutUser = async () => {
  console.log("Starting logout API call...");
  
  try {
    const url = `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/session/deactivate/`;
    console.log("Logout URL:", url);
    console.log("Cookies before logout:", document.cookie);
    
    const response = await fetch(url, {
      method: "PUT",
      headers: {
        "Content-Type": "application/json",
      },
      credentials: "include", // Include cookies for auth token
    });
    
    console.log("Logout response status:", response.status);
    console.log("Logout response headers:", response.headers);
    
    // Parse response if available
    let responseBody = {};
    try {
      responseBody = await response.json();
      console.log("📦 Logout response body:", responseBody);
    } catch (parseError) {
      console.warn("Could not parse logout response:", parseError);
    }

    console.log("Cookies after logout:", document.cookie);

    if (!response.ok) {
      console.warn("Logout API failed with status:", response.status, responseBody);
      return {
        ok: false,
        error: responseBody.message || `HTTP ${response.status}`,
        status: response.status
      };
    }

    console.log("Logout API succeeded");
    return {
      ok: true,
      message: "Session deactivated successfully",
    };
  } catch (error) {
    console.error("Error during logout API call:", error);
    return {
      ok: false,
      error: error.message || "Network error during logout",
    };
  }
};


// ===== MODEL CALIBRATION ANALYTICS FUNCTIONS =====

/**
 * Get reliability diagram data (bins of predicted confidence vs empirical accuracy)
 * @param {Object} params - { start_time, end_time, model_id, bins, user_id }
 */
export const getReliabilityDiagram = async (params = {}) => {
  try {
    const queryParams = new URLSearchParams({
      bins: params.bins || 10,
      ...params,
    }).toString();

    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/analytics/calibration/reliability-diagram?${queryParams}`,
      {
        method: "GET",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
      }
    );
    const responseBody = await response.json();

    if (!response.ok) {
      return { ok: false, error: responseBody["detail"] || `${response.status}: ${response.statusText}` };
    }
    return { ok: true, data: responseBody };
  } catch (error) {
    console.error("Error fetching reliability diagram:", error);
    return { ok: false, error: "An unexpected error occurred. Please try again." };
  }
};

/**
 * Get Brier score summary
 * @param {Object} params - { start_time, end_time, model_id, group_by, user_id }
 */
export const getBrierScore = async (params = {}) => {
  try {
    const queryParams = new URLSearchParams({
      group_by: params.group_by || "model",
      ...params,
    }).toString();

    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/analytics/calibration/brier-score?${queryParams}`,
      {
        method: "GET",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
      }
    );
    const responseBody = await response.json();

    if (!response.ok) {
      return { ok: false, error: responseBody["detail"] || `${response.status}: ${response.statusText}` };
    }
    return { ok: true, data: responseBody };
  } catch (error) {
    console.error("Error fetching Brier score:", error);
    return { ok: false, error: "An unexpected error occurred. Please try again." };
  }
};

/**
 * Get confidence score distribution (histogram)
 * @param {Object} params - { start_time, end_time, model_id, bins, user_id }
 */
export const getConfidenceDistribution = async (params = {}) => {
  try {
    const queryParams = new URLSearchParams({
      bins: params.bins || 20,
      ...params,
    }).toString();

    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/analytics/calibration/confidence-distribution?${queryParams}`,
      {
        method: "GET",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
      }
    );
    const responseBody = await response.json();

    if (!response.ok) {
      return { ok: false, error: responseBody["detail"] || `${response.status}: ${response.statusText}` };
    }
    return { ok: true, data: responseBody };
  } catch (error) {
    console.error("Error fetching confidence distribution:", error);
    return { ok: false, error: "An unexpected error occurred. Please try again." };
  }
};

/**
 * Get calibration summary across models
 * @param {Object} params - { start_time, end_time, user_id }
 */
export const getCalibrationSummary = async (params = {}) => {
  try {
    const queryParams = new URLSearchParams({
      ...params,
    }).toString();

    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/analytics/calibration/calibration-summary?${queryParams}`,
      {
        method: "GET",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
      }
    );
    const responseBody = await response.json();

    if (!response.ok) {
      return { ok: false, error: responseBody["detail"] || `${response.status}: ${response.statusText}` };
    }
    return { ok: true, data: responseBody };
  } catch (error) {
    console.error("Error fetching calibration summary:", error);
    return { ok: false, error: "An unexpected error occurred. Please try again." };
  }
};


// ===== ADMIN-ONLY CONFIG MANAGEMENT FUNCTIONS =====

export const listConfigs = async () => {
  try {
    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/config/`,
      {
        method: "GET",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
      },
    );
    const body = await response.json();
    if (!response.ok) {
      return { ok: false, error: body["detail"] || `${response.status}: ${response.statusText}` };
    }
    return { ok: true, data: body.configs || [] };
  } catch (e) {
    console.error("Error listing configs:", e);
    return { ok: false, error: "Failed to load configs" };
  }
};

export const getConfigById = async (configId) => {
  try {
    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/config/${configId}`,
      { method: "GET", headers: { "Content-Type": "application/json" }, credentials: "include" },
    );
    const body = await response.json();
    if (!response.ok) {
      return { ok: false, error: body["detail"] || `${response.status}: ${response.statusText}` };
    }
    return { ok: true, data: body };
  } catch (e) {
    console.error("Error getting config:", e);
    return { ok: false, error: "Failed to get config" };
  }
};

export const createConfigApi = async (configData) => {
  try {
    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/config/`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ config_data: configData }),
      },
    );
    const body = await response.json();
    if (!response.ok) {
      return { ok: false, error: body["detail"] || `${response.status}: ${response.statusText}` };
    }
    return { ok: true, data: body };
  } catch (e) {
    console.error("Error creating config:", e);
    return { ok: false, error: "Failed to create config" };
  }
};

export const updateConfigApi = async (configId, configData) => {
  try {
    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/config/${configId}`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ config_data: configData }),
      },
    );
    const body = await response.json();
    if (!response.ok) {
      return { ok: false, error: body["detail"] || `${response.status}: ${response.statusText}` };
    }
    return { ok: true, data: body };
  } catch (e) {
    console.error("Error updating config:", e);
    return { ok: false, error: "Failed to update config" };
  }
};

export const deleteConfigApi = async (configId) => {
  try {
    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/config/${configId}`,
      { method: "DELETE", headers: { "Content-Type": "application/json" }, credentials: "include" },
    );
    const body = await response.json();
    if (!response.ok) {
      return { ok: false, error: body["detail"] || `${response.status}: ${response.statusText}` };
    }
    return { ok: true, data: body };
  } catch (e) {
    console.error("Error deleting config:", e);
    return { ok: false, error: "Failed to delete config" };
  }
};

export const getLanguagesMapping = async () => {
  try {
    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/config/languages`,
      { method: "GET", headers: { "Content-Type": "application/json" }, credentials: "include" },
    );
    const body = await response.json();
    if (!response.ok) {
      return { ok: false, error: body["detail"] || `${response.status}: ${response.statusText}` };
    }
    return { ok: true, data: body };
  } catch (e) {
    console.error("Error fetching languages:", e);
    return { ok: false, error: "Failed to load languages" };
  }
};

export const getAvailableModels = async () => {
  try {
    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/config/models`,
      { method: "GET", headers: { "Content-Type": "application/json" }, credentials: "include" },
    );
    const body = await response.json();
    if (!response.ok) {
      return { ok: false, error: body["detail"] || `${response.status}: ${response.statusText}` };
    }
    return { ok: true, data: body.models || [] };
  } catch (e) {
    console.error("Error fetching available models:", e);
    return { ok: false, error: "Failed to load models" };
  }
};

export const validateHuggingFaceModel = async (name) => {
  if (!name || !name.trim()) return { ok: true, exists: false };
  try {
    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/config/models/validate?name=${encodeURIComponent(name)}`,
      { method: "GET", headers: { "Content-Type": "application/json" }, credentials: "include" },
    );
    const body = await response.json();
    if (!response.ok) {
      return { ok: false, error: body["detail"] || `${response.status}: ${response.statusText}` };
    }
    return { ok: true, exists: !!body.exists, status_code: body.status_code, error: body.error };
  } catch (e) {
    console.error("Error validating HF model:", e);
    return { ok: false, error: "Validation failed" };
  }
};

// ── Agent subsystem ─────────────────────────────────────────────────────────
//
// Backs the three admin pages: AgentProfiles (define experiment arms),
// AgentAssignments (inspect / pin the A/B buckets), and AgentResults
// (compare arms). All endpoints are admin-only server-side.

const AGENT_BASE = () =>
  `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/agent`;

// Shared request helper. The hand-rolled fetch blocks above predate it; new
// agent endpoints funnel through here so error handling stays consistent.
//
// Error bodies are normalized so no raw object/array ever reaches JSX:
//   * FastAPI's default 422 `detail` array (`{type,loc,msg,input}`) becomes
//     `errors: [{field, message, code}]` (loc joined with "."), with a readable
//     summary string;
//   * a custom `detail` object `{code, field, message}` becomes one entry;
//   * a plain `detail`/`message` string is used as-is.
// The same helpers back `researchRequest`, so there is one pattern, not two.
const normalizeErrorEntries = (payload) => {
  if (!payload || typeof payload !== "object") return [];
  const detail = payload.detail;
  if (Array.isArray(detail)) {
    return detail.map((item) => {
      if (typeof item === "string") return { field: "", message: item, code: "" };
      const loc = Array.isArray(item.loc) ? item.loc.join(".") : "";
      return {
        field: item.field || loc || "",
        message: item.msg || item.message || "",
        code: item.code || item.type || "",
        severity: item.severity,
      };
    });
  }
  if (Array.isArray(payload.errors)) return payload.errors;
  if (detail && typeof detail === "object") {
    return [
      {
        field: detail.field || "",
        message: detail.message || detail.msg || detail.reason || "",
        code: detail.code || "",
        severity: detail.severity,
      },
    ];
  }
  return [];
};

const normalizeRequestError = (payload, response) => {
  const detail = payload ? payload.detail : undefined;
  const errors = normalizeErrorEntries(payload);
  const detailObject =
    detail && typeof detail === "object" && !Array.isArray(detail) ? detail : null;
  const detailMessage =
    (typeof detail === "string" && detail) ||
    (detailObject &&
      (detailObject.message || detailObject.code || detailObject.reason)) ||
    (payload && payload.message) ||
    "";
  // A typed validation failure must never surface as
  // "422 Unprocessable Entity": summarize the field errors instead.
  const error =
    detailMessage ||
    (errors.length
      ? summarizeValidationErrors(errors)
      : `${response.status}: ${response.statusText}`);
  return { error, errors, status: response.status };
};

const agentRequest = async (path, { method = "GET", body, label } = {}) => {
  try {
    const response = await fetch(`${AGENT_BASE()}${path}`, {
      method,
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
    });
    // 204 has no body to parse.
    const payload =
      response.status === 204 ? {} : await response.json().catch(() => ({}));
    if (!response.ok) {
      return { ok: false, ...normalizeRequestError(payload, response) };
    }
    return { ok: true, data: payload, status: response.status };
  } catch (e) {
    console.error(`Error ${label || path}:`, e);
    return {
      ok: false,
      error: `Failed to ${label || "complete request"}`,
      errors: [],
    };
  }
};

export const getAgentProfiles = async () => {
  const result = await agentRequest("/profiles", { label: "load agent profiles" });
  return result.ok ? { ok: true, data: result.data.profiles || [] } : result;
};

export const createAgentProfile = async (profile) =>
  agentRequest("/profiles", {
    method: "POST",
    body: profile,
    label: "create agent profile",
  });

export const updateAgentProfile = async (profileId, profile) =>
  agentRequest(`/profiles/${profileId}`, {
    method: "PUT",
    body: profile,
    label: "update agent profile",
  });

export const deleteAgentProfile = async (profileId) =>
  agentRequest(`/profiles/${profileId}`, {
    method: "DELETE",
    label: "delete agent profile",
  });

// Tool names selectable for a profile. Pass a frameworkVersion to narrow the
// list to the runtime the profile targets.
export const getAgentAvailableTools = async (frameworkVersion) => {
  const query = frameworkVersion
    ? `?framework_version=${encodeURIComponent(frameworkVersion)}`
    : "";
  const result = await agentRequest(`/available-tools${query}`, {
    label: "load available agent tools",
  });
  return result.ok
    ? {
        ok: true,
        data: {
          tools: result.data.tools || [],
          frameworks: result.data.frameworks || [],
        },
      }
    : result;
};

export const getAgentAssignments = async () => {
  const result = await agentRequest("/assignments", {
    label: "load agent assignments",
  });
  return result.ok
    ? { ok: true, data: result.data.assignments || [] }
    : result;
};

export const getAgentAssignmentOptions = async () => {
  const result = await agentRequest("/assignment-options", {
    label: "load assignment options",
  });
  return result.ok ? { ok: true, data: result.data } : result;
};

// Add a manual assignment to a study arm.
export const setAgentAssignment = async (userId, profileId, studyId) =>
  agentRequest(`/assignments/${userId}`, {
    method: "PUT",
    body: {
      profile_id: profileId,
      ...(studyId ? { study_id: studyId } : {}),
    },
    label: "set agent assignment",
  });

// Clear an assignment so the user is re-drawn on their next agent task.
export const deleteAgentAssignment = async (userId, studyId) =>
  agentRequest(
    `/assignments/${userId}${studyId ? `?study_id=${encodeURIComponent(studyId)}` : ""}`,
    {
      method: "DELETE",
      label: "clear agent assignment",
    },
  );

// ── Research control plane ──────────────────────────────────────────────────
//
// Backs the researcher-facing authoring pages (ResearchStudies,
// ResearchStudyEditor, ResearchEnrollment). The backend mounts these under
// /api/research and every endpoint is admin/authorized-researcher only, so they
// reuse the existing cookie-based session like the analytics/admin helpers.
//
// Validation failures from the protocol service are 422 responses whose
// `detail` is a *list* of {code, field, message, severity} entries. Those are
// preserved on `errors` so the UI can render field-level messages instead of
// collapsing them into one opaque string.

const RESEARCH_BASE = () =>
  `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/research`;

const summarizeValidationErrors = (errors) =>
  errors
    .slice(0, 3)
    .map((error) => {
      if (typeof error === "string") return error;
      return `${error.field || error.code || "protocol"}: ${error.message || ""}`.trim();
    })
    .join("; ");

export const researchRequest = async (
  path,
  { method = "GET", body, label } = {},
) => {
  try {
    const response = await fetch(`${RESEARCH_BASE()}${path}`, {
      method,
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
    });
    // 204 has no body to parse.
    const payload =
      response.status === 204 ? {} : await response.json().catch(() => ({}));
    if (!response.ok) {
      return { ok: false, ...normalizeRequestError(payload, response) };
    }
    return { ok: true, data: payload, status: response.status };
  } catch (e) {
    console.error(`Error ${label || path}:`, e);
    return {
      ok: false,
      error: `Failed to ${label || "complete request"}`,
      errors: [],
    };
  }
};

/**
 * List study identities.
 *
 * NOTE: the study protocol router currently exposes only
 * `/studies/drafts` and `/studies/revisions`, both of which require an
 * explicit `study_id`. A study index is therefore not guaranteed to exist; the
 * call is made optimistically and a 404/405 is reported as `missing` so the UI
 * can fall back to manual study-id entry instead of failing hard.
 */
/**
 * Create a study identity.
 *
 * The server generates the UUID (and grants the caller OWNER so they can
 * author it immediately), which removes the "paste a UUID" step from the UI.
 * The identity materializes with the study's first draft/revision.
 */
export const createResearchStudy = async ({ name, description, owner } = {}) =>
  researchRequest("/studies", {
    method: "POST",
    body: {
      name,
      description: description ?? null,
      owner: owner ?? null,
    },
    label: "create research study",
  });

export const listResearchStudies = async () => {
  const result = await researchRequest("/studies", {
    label: "load research studies",
  });
  if (result.ok) {
    const data = result.data || {};
    return { ok: true, data: data.studies || data || [] };
  }
  if (result.status === 403) {
    return {
      ok: false,
      forbidden: true,
      error:
        "Your account is not enabled for research. Ask an administrator to enable researcher access.",
    };
  }
  if (result.status === 404 || result.status === 405) {
    return {
      ok: false,
      missing: true,
      error: "The study-listing endpoint is not available on this server yet.",
    };
  }
  return result;
};

const RESEARCH_ENDPOINT_MISSING = (status) => status === 404 || status === 405;

// Study-scoped join code: the value a researcher shares with participants. It is
// bound to the study's latest published revision, not to the researcher's own
// account, so it replaces the per-account enrollment id as the "share this"
// value on the enrollment page. The endpoint is newer than the rest of the
// control plane; a 404/405 is reported as `missing` so the UI can degrade to the
// per-account enrollment id instead of failing.
export const getResearchStudyJoinCode = async (studyId) => {
  const result = await researchRequest(
    `/studies/${encodeURIComponent(studyId)}/join-code`,
    { label: "load study join code" },
  );
  if (result.ok) {
    const data = result.data || {};
    // The endpoint returns the join code plus a nested ``revision`` summary.
    const revision = data.revision || {};
    return {
      ok: true,
      join_code: data.join_code || "",
      revision_id: data.revision_id || revision.revision_id || "",
      revision_number: revision.revision_number ?? null,
      status: data.status || revision.status || "",
      protocol_digest: revision.protocol_digest || "",
    };
  }
  if (RESEARCH_ENDPOINT_MISSING(result.status)) {
    return {
      ok: false,
      missing: true,
      error: "The study join-code endpoint is not available on this server yet.",
    };
  }
  return result;
};

// Resolve a participant join code to the study/revision it points at, without
// redeeming it. Read-only, so a participant can confirm the study before
// accepting the policy and the join page can compare it with any existing enrollment.
export const resolveResearchJoinCode = async (joinCode) => {
  const result = await researchRequest(
    `/join/${encodeURIComponent(joinCode)}`,
    { label: "resolve study join code" },
  );
  if (result.ok) {
    const data = result.data || {};
    const study = data.study || {};
    const revision = data.revision || {};
    const consent = data.consent || {};
    return {
      ok: true,
      data: {
        joinCode: data.join_code || joinCode,
        study: {
          studyId: study.study_id || "",
          name: study.name || "",
        },
        revision: {
          revisionId: revision.revision_id || "",
          revisionNumber: revision.revision_number ?? null,
          status: revision.status || "",
          protocolDigest: revision.protocol_digest || "",
          publishedAt: revision.published_at || null,
        },
        // The single global policy text the participant accepts once at join.
        policyText: consent.text || "",
      },
    };
  }
  if (RESEARCH_ENDPOINT_MISSING(result.status)) {
    return {
      ok: false,
      missing: true,
      error:
        "That join code could not be found, or the join service is not available on this server yet.",
    };
  }
  return result;
};

// Redeem a participant join code for the signed-in account. Idempotent
// server-side: re-running reuses the existing enrollment.
export const redeemResearchJoinCode = async (joinCode, acceptConsent) => {
  const result = await researchRequest("/join", {
    method: "POST",
    body: { join_code: joinCode, accept_consent: !!acceptConsent },
    label: "redeem study join code",
  });
  if (result.ok) {
    const data = result.data || {};
    return {
      ok: true,
      data: {
        enrollment_id: data.enrollment_id || "",
        study_id: data.study_id || "",
        revision_id: data.revision_id || "",
        status: data.status || "",
      },
    };
  }
  if (RESEARCH_ENDPOINT_MISSING(result.status)) {
    // Same ambiguity as resolve: unknown/expired code or an unmounted service.
    return {
      ok: false,
      missing: true,
      error:
        "That join code could not be redeemed, or the join service is not available on this server yet.",
    };
  }
  return result;
};

// The signed-in account's own enrollment projections (study-local, no account
// data). Used by the join page to avoid re-asking for the policy when an active
// enrollment already exists for the code's revision.
export const getMyResearchEnrollments = async () => {
  const result = await researchRequest("/participants/me", {
    label: "load my research enrollments",
  });
  if (result.ok) {
    const data = result.data || {};
    return { ok: true, data: data.enrollments || [] };
  }
  if (RESEARCH_ENDPOINT_MISSING(result.status)) {
    return {
      ok: false,
      missing: true,
      error:
        "Your enrollment status is not available on this server yet.",
    };
  }
  return result;
};

export const listResearchRevisions = async (studyId) => {
  const result = await researchRequest(
    `/studies/revisions?study_id=${encodeURIComponent(studyId)}`,
    { label: "load study revisions" },
  );
  return result.ok
    ? { ok: true, data: result.data.revisions || [] }
    : result;
};

export const getResearchRevision = async (revisionId) => {
  const result = await researchRequest(
    `/studies/revisions/${encodeURIComponent(revisionId)}`,
    { label: "load study revision" },
  );
  return result.ok
    ? { ok: true, revision: result.data.revision, protocol: result.data.protocol }
    : result;
};

export const listResearchDrafts = async (studyId) => {
  const result = await researchRequest(
    `/studies/drafts?study_id=${encodeURIComponent(studyId)}`,
    { label: "load study drafts" },
  );
  return result.ok ? { ok: true, data: result.data.drafts || [] } : result;
};

export const getResearchDraft = async (draftId) => {
  const result = await researchRequest(
    `/studies/drafts/${encodeURIComponent(draftId)}`,
    { label: "load study draft" },
  );
  return result.ok
    ? {
        ok: true,
        draft_id: result.data.draft_id,
        study_id: result.data.study_id,
        name: result.data.name,
        protocol: result.data.protocol,
      }
    : result;
};

export const createResearchDraft = async ({ studyId, name, protocol }) =>
  researchRequest("/studies/drafts", {
    method: "POST",
    body: { study_id: studyId, name, protocol },
    label: "create study draft",
  });

export const validateResearchProtocol = async (protocol) => {
  const result = await researchRequest("/studies/drafts/validate", {
    method: "POST",
    body: { protocol },
    label: "validate study protocol",
  });
  if (result.ok) {
    return {
      ok: true,
      valid: result.data.valid !== false,
      errors: result.data.errors || [],
      warnings: result.data.warnings || [],
    };
  }
  if (result.status === 422) {
    return {
      ok: false,
      valid: false,
      errors: result.errors || [],
      warnings: result.data ? result.data.warnings || [] : [],
      error: result.error,
    };
  }
  return {
    ...result,
    valid: false,
    errors: result.errors || [],
    warnings: [],
  };
};

export const publishResearchDraft = (draftId, payload) =>
  researchRequest(`/studies/drafts/${encodeURIComponent(draftId)}/publish`, {
    method: "POST",
    body: payload,
    label: "publish study draft",
  });

export const supersedeResearchRevision = (revisionId, payload) =>
  researchRequest(
    `/studies/revisions/${encodeURIComponent(revisionId)}/supersede`,
    {
      method: "POST",
      body: payload,
      label: "supersede study revision",
    },
  );

export const retireResearchRevision = (revisionId, actor) =>
  researchRequest(`/studies/revisions/${encodeURIComponent(revisionId)}/retire`, {
    method: "POST",
    body: { actor: actor ?? null },
    label: "retire study revision",
  });

// Derived read models (researcher control plane). Missing coverage is returned
// with an explicit coverage state; the UI must render "unavailable", not zero.
export const getResearchExposures = async (studyId, revisionId) => {
  const result = await researchRequest(
    `/operations/exposures?study_id=${encodeURIComponent(studyId)}&revision_id=${encodeURIComponent(revisionId)}`,
    { label: "load condition exposures" },
  );
  return result.ok ? { ok: true, data: result.data.conditions || [] } : result;
};

export const getResearchEnrollmentCoverage = async (studyId, revisionId) => {
  const result = await researchRequest(
    `/operations/enrollments/coverage?study_id=${encodeURIComponent(studyId)}&revision_id=${encodeURIComponent(revisionId)}`,
    { label: "load enrollment coverage" },
  );
  return result.ok ? { ok: true, data: result.data } : result;
};

// Registered agent releases (admin-only; digest-pinned artifacts). Used by the
// editor/enrollment views to distinguish packaged agents from BYOA setups.
export const getAgentReleases = async () => {
  const result = await researchRequest("/agents/releases", {
    label: "load agent releases",
  });
  return result.ok ? { ok: true, data: result.data.releases || [] } : result;
};

// Derived, read-only distribution views. A profile IS a distribution; every
// entry carries the resolved `release_id`/`release_version`, the server-derived
// `verified` flag and the release's `supported_platforms`. Readable by any
// authenticated user and exposes no secret, so a researcher can pick exactly one
// distribution without ever seeing a release id or digest as an input.
export const getAgentDistributions = async () => {
  const result = await researchRequest("/agents/distributions", {
    label: "load agent distributions",
  });
  return result.ok
    ? { ok: true, data: result.data.distributions || [] }
    : result;
};

export const getAgentDistribution = async (distributionId) => {
  const result = await researchRequest(
    `/agents/distributions/${encodeURIComponent(distributionId)}`,
    { label: "load agent distribution" },
  );
  return result.ok ? { ok: true, data: result.data.distribution } : result;
};

// Provider connections the caller may select for a profile. Administrators see
// every connection (including endpoint/secret_ref); researchers see only the
// connections granted to them. The response never includes a secret value.
export const getProviderConnections = async () => {
  const result = await researchRequest("/provider-connections", {
    label: "load provider connections",
  });
  return result.ok
    ? { ok: true, data: result.data.connections || [] }
    : result;
};

// One registered release with its full digest-pinned artifact and adapter
// metadata. The list endpoint returns a compact summary, so the release picker
// resolves the selected release to auto-fill version/digest/adapter_version.
export const getAgentRelease = async (releaseId) => {
  const result = await researchRequest(
    `/agents/releases/${encodeURIComponent(releaseId)}`,
    { label: "load agent release" },
  );
  return result.ok
    ? { ok: true, release: result.data.release, model: result.data.model }
    : result;
};

export const getResearchPackages = async (releaseId) => {
  const query = releaseId
    ? `?release_id=${encodeURIComponent(releaseId)}`
    : "";
  const result = await researchRequest(`/packages${query}`, {
    label: "load runtime packages",
  });
  return result.ok ? { ok: true, data: result.data.packages || [] } : result;
};

// Enroll the *current* account in a published revision and return the
// researcher-safe enrollment projection (whose enrollment_id is the join code
// surfaced on the enrollment page). Idempotent server-side: re-running reuses
// the existing enrollment.
export const requestResearchEnrollment = async (studyId, revisionId) =>
  researchRequest("/participants/enrollments", {
    method: "POST",
    body: { study_id: studyId, revision_id: revisionId },
    label: "request study enrollment",
  });

export const getResearchEnrollment = async (enrollmentId) => {
  const result = await researchRequest(
    `/participants/enrollments/${encodeURIComponent(enrollmentId)}`,
    { label: "load enrollment" },
  );
  return result.ok ? { ok: true, data: result.data.enrollment } : result;
};

// Per-arm comparison for a study's agent arms.
export const getStudyAgentEvaluation = async (studyId) => {
  try {
    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/analytics/studies/${studyId}/agent-evaluation`,
      {
        method: "GET",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
      },
    );
    const body = await response.json();
    if (!response.ok) {
      return {
        ok: false,
        error: body["detail"] || `${response.status}: ${response.statusText}`,
      };
    }
    return { ok: true, data: body };
  } catch (e) {
    console.error("Error loading agent evaluation:", e);
    return { ok: false, error: "Failed to load agent evaluation" };
  }
};
