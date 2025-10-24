from agent.utils.baselib_gateway import baselib_view, jpsBaseLibGW


class JavaTimeParser:
    def __init__(self):
        self._method_cache = {}

        # Cache this once — reused every call
        self.char_seq_class = baselib_view.java.lang.Class.forName(
            "java.lang.CharSequence")
        self.param_types = jpsBaseLibGW.gateway.new_array(
            baselib_view.java.lang.Class, 1)
        self.param_types[0] = self.char_seq_class
        self.object_class = baselib_view.java.lang.Object

    def parse_java_time(self, class_name: str, time_str: str):
        # 1. Check cache first
        if class_name not in self._method_cache:
            time_class = baselib_view.java.lang.Class.forName(class_name)
            parse_method = time_class.getMethod("parse", self.param_types)
            self._method_cache[class_name] = parse_method
        else:
            parse_method = self._method_cache[class_name]

        # 2. Build argument array
        args_array = jpsBaseLibGW.gateway.new_array(self.object_class, 1)
        args_array[0] = baselib_view.java.lang.String(time_str)

        # 3. Invoke static parse method
        return parse_method.invoke(None, args_array)
