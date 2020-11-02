# sardana-adlink
Repository for the Sardana plugins related to AdLink

### AdlinkAIOneDCtrl
- Multiple HW trigger on each step.
- Can't send multiple Software Triggers is SW Synchronization, so will always be one point 1D.
- `PointsPerStep` for how many points per step. Should correspond to the incoming triggers per step and should be configured before the scan.