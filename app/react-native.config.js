// The app does not use gesture-handler, and its native paths exceed the Windows path limit.
module.exports = {
  dependencies: {
    'react-native-gesture-handler': { platforms: { android: null } },
  },
};
