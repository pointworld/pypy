#ifndef DATETIME_H
#define DATETIME_H
#ifdef __cplusplus
extern "C" {
#endif

/* Define structure for C API. */
typedef struct {
    /* type objects */
    PyTypeObject *DateType;
    PyTypeObject *DateTimeType;
    PyTypeObject *TimeType;
    PyTypeObject *DeltaType;
    PyTypeObject *TZInfoType;
} PyDateTime_CAPI;

PyAPI_DATA(PyDateTime_CAPI*) PyDateTimeAPI;
#define PyDateTime_IMPORT                           \
    do {                                            \
        if(PyDateTimeAPI==NULL)                     \
            PyDateTimeAPI = _PyDateTime_Import();   \
    } while (0)

typedef struct {
    PyObject_HEAD
} PyDateTime_Delta;

typedef struct {
    PyObject_HEAD
} PyDateTime_Date;

typedef struct {
    PyObject_HEAD
} PyDateTime_Time;

typedef struct {
    PyObject_HEAD
} PyDateTime_DateTime;

typedef struct {
    PyObject_HEAD
} PyDateTime_TZInfo;

#ifdef __cplusplus
}
#endif
#endif
